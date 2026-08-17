# -*- coding: utf-8 -*-
"""
Расчётная модель юнит-экономики системы речевой аналитики автодилера.
Цены — веб-исследование августа 2026 (см. ../research/*.md), помечены источниками.

Запуск: python model.py
"""

import math
from dataclasses import dataclass

FX = 83.0  # ₽/$, ЦБ середина августа 2026 [research/cloud.md]

# ----------------------------------------------------------------------------
# ДОПУЩЕНИЯ
# ----------------------------------------------------------------------------

A = {
    # --- нагрузка ---
    "conv_per_shift": 6.5,        # 5–8 разговоров за смену (середина)
    "conv_min_avg": 27.0,         # 15–40 мин (середина)
    "shifts_per_month": 22,
    "speech_ratio": 0.70,         # доля речи после VAD
    "peak_talk_share": 0.60,      # доля менеджеров, говорящих одновременно в пиковый час

    # --- аудио ---
    "opus_kbps": 24.0,            # Opus mono 16 кГц

    # --- обработка, RTFx на GPU класса RTX 4090 24GB [research/asr.md] ---
    # GigaAM-v3: RTF 0.033 один поток (30x), несколько воркеров на 24 ГБ -> агрегированно
    "asr_rtfx": {"cons": 40.0, "opt": 100.0},
    # pyannote community-1 с батчингом и канальным приором (диар только по речи)
    "diar_rtfx": {"cons": 15.0, "opt": 50.0},
    # LLM T-Pro 2.0 32B int4 на 4090: 180-300 бесед/час [research/llm.md]
    "llm_conv_per_gpu_hour": {"cons": 180.0, "opt": 300.0},

    # --- GPU, ₽/мес за выделенный сервер класса 4090 [research/cloud.md] ---
    "gpu_month_rub": {"cons": 56_900.0,   # Selectel выделенный RTX 4090 [точно]
                      "opt": 29_000.0},   # HOSTKEY VPS RTX 4090 [точно по обзору]
    "gpu_hour_rub": 82.76,        # immers.cloud RTX 4090 почасово [точно] — для burst
    "gpu_util_target": 0.80,      # планируем загрузку не выше 80% (запас на пики/ретраи)

    # --- LLM через API (альтернатива self-hosted) [research/llm.md] ---
    "llm_api_rub_per_conv": 3.38, # GigaChat Pro batch, 13.5 тыс. ток. [точно]

    # --- хранение [research/cloud.md] ---
    "retention_months_audio": 6,  # юр. рекомендация 3–6 мес [research/legal.md]
    "storage_rub_gb_std": 2.30,
    "storage_rub_gb_cold": 1.10,

    # --- бэкенд-фикс, ₽/мес [research/cloud.md: ВМ 5.8k, PG 5.5-16k, +мониторинг] ---
    "backend_fix_rub": {100: 35_000.0, 1000: 120_000.0},
    "lte_per_site_rub": 1_000.0,  # SIM 35-150 ГБ [research/hardware.md]

    # --- CAPEX, $ [research/hardware.md] ---
    "badge_usd": {100: 52.0, 1000: 31.0},   # BOM+15% запас
    "spare_ratio": 0.10,
    "dock_per_mgr_usd": 12.0,               # мульти-док 10 слотов ~$120/10
    "gateway_per_site_usd": 585.0,          # N100 26.9k + hAP ax2 12.5k + Keenetic 9k ₽ / 83
    "mgr_per_site": 20,
}


@dataclass
class Scale:
    managers: int
    sites: int


def audio_h_day_mgr() -> float:
    return A["conv_per_shift"] * A["conv_min_avg"] / 60.0


def audio_h_month_mgr() -> float:
    return audio_h_day_mgr() * A["shifts_per_month"]


def opus_mb_per_hour() -> float:
    return A["opus_kbps"] / 8.0 * 3600.0 / 1024.0


def peak_hour(s: Scale) -> dict:
    conc = s.managers * A["peak_talk_share"]
    return {
        "streams": conc,
        "uplink_mbit": conc * A["opus_kbps"] / 1000.0,
        "speech_hours": conc * A["speech_ratio"],
    }


def gpu_plan(s: Scale, sc: str) -> dict:
    """Планирование GPU: ASR+диаризация днём, LLM — в свободные часы тех же GPU."""
    h_audio = audio_h_day_mgr() * s.managers          # часов аудио в сутки
    h_speech = h_audio * A["speech_ratio"]
    gpu_h_asr = h_speech / A["asr_rtfx"][sc]
    gpu_h_diar = h_speech / A["diar_rtfx"][sc]
    conv_day = A["conv_per_shift"] * s.managers
    gpu_h_llm = conv_day / A["llm_conv_per_gpu_hour"][sc]

    gpu_h_total = gpu_h_asr + gpu_h_diar + gpu_h_llm
    capacity_per_gpu = 24.0 * A["gpu_util_target"]

    # Оптимизатор закупки: N выделенных + почасовой burst на перелив.
    n_max = math.ceil(gpu_h_total / capacity_per_gpu)
    best = None
    for n_ded in range(0, n_max + 1):
        overflow_h_day = max(0.0, gpu_h_total - n_ded * capacity_per_gpu)
        cost = n_ded * A["gpu_month_rub"][sc] + overflow_h_day * 30.4 * A["gpu_hour_rub"]
        if best is None or cost < best["cost_rub"]:
            best = {"n_ded": n_ded, "overflow_h_day": overflow_h_day, "cost_rub": cost}

    fleet_eq = max(1, n_max)  # эквивалент парка для оценки скорости разбора пика
    return {
        "audio_h_day": h_audio, "speech_h_day": h_speech,
        "gpu_h_asr": gpu_h_asr, "gpu_h_diar": gpu_h_diar, "gpu_h_llm": gpu_h_llm,
        "gpu_h_total": gpu_h_total, "conv_day": conv_day,
        "n_gpu": fleet_eq, **best,
    }


def peak_drain(s: Scale, sc: str, n_gpu: int) -> dict:
    """Пиковый час: сколько часов речи прилетает и за сколько парк её разберёт."""
    ph = peak_hour(s)
    gpu_h_needed = ph["speech_hours"] / A["asr_rtfx"][sc] + ph["speech_hours"] / A["diar_rtfx"][sc]
    drain_min = gpu_h_needed / n_gpu * 60.0
    return {"gpu_h_needed": gpu_h_needed, "drain_min": drain_min, **ph}


def storage(s: Scale) -> dict:
    gb_new = audio_h_month_mgr() * opus_mb_per_hour() / 1024.0 * s.managers
    gb_steady = gb_new * A["retention_months_audio"]
    cost = gb_new * A["storage_rub_gb_std"] + (gb_steady - gb_new) * A["storage_rub_gb_cold"]
    return {"gb_new": gb_new, "gb_steady": gb_steady, "cost_rub": cost}


def monthly(s: Scale, sc: str) -> dict:
    g = gpu_plan(s, sc)
    st = storage(s)
    conv_month = A["conv_per_shift"] * A["shifts_per_month"] * s.managers
    llm_api = conv_month * A["llm_api_rub_per_conv"]  # альтернатива self-hosted
    backend = A["backend_fix_rub"][100 if s.managers <= 300 else 1000]
    lte = A["lte_per_site_rub"] * s.sites
    total = g["cost_rub"] + st["cost_rub"] + backend + lte
    return {
        "gpu": g, "storage": st, "conv_month": conv_month,
        "llm_api_alt_rub": llm_api,
        "backend": backend, "lte": lte,
        "total_rub": total,
        "per_mgr_rub": total / s.managers,
        "per_mgr_usd": total / s.managers / FX,
    }


def capex(s: Scale) -> dict:
    badge = A["badge_usd"][100 if s.managers <= 300 else 1000]
    badge_sp = badge * (1 + A["spare_ratio"])
    gw = A["gateway_per_site_usd"] / A["mgr_per_site"]
    per_mgr = badge_sp + A["dock_per_mgr_usd"] + gw
    return {"badge": badge, "badge_sp": badge_sp, "dock": A["dock_per_mgr_usd"],
            "gw": gw, "per_mgr": per_mgr, "fleet": per_mgr * s.managers}


def report():
    print(f"Курс: {FX} ₽/$")
    print(f"Аудио: {audio_h_day_mgr():.2f} ч/смена/менеджер, {audio_h_month_mgr():.1f} ч/мес; "
          f"Opus {A['opus_kbps']:.0f} кбит/с = {opus_mb_per_hour():.1f} МБ/ч "
          f"({audio_h_month_mgr()*opus_mb_per_hour()/1024:.2f} ГБ/мес/менеджер)\n")

    for s in (Scale(100, 5), Scale(1000, 50)):
        print("=" * 78)
        print(f"МАСШТАБ {s.managers} менеджеров / {s.sites} площадок")
        ph = peak_hour(s)
        print(f"  Пик: {ph['streams']:.0f} одновременных разговоров, аплинк {ph['uplink_mbit']:.1f} Мбит/с")
        for sc, name in (("cons", "КОНСЕРВАТИВНО"), ("opt", "ОПТИМИСТИЧНО")):
            m = monthly(s, sc)
            g = m["gpu"]
            pd = peak_drain(s, sc, g["n_gpu"])
            print(f"  --- {name} (RTFx ASR {A['asr_rtfx'][sc]:.0f}, диар {A['diar_rtfx'][sc]:.0f}, "
                  f"GPU {A['gpu_month_rub'][sc]:,.0f} ₽/мес) ---")
            print(f"    Сутки: {g['audio_h_day']:.0f} ч аудио / {g['speech_h_day']:.0f} ч речи; "
                  f"GPU-часы: ASR {g['gpu_h_asr']:.1f} + диар {g['gpu_h_diar']:.1f} + LLM {g['gpu_h_llm']:.1f} "
                  f"= {g['gpu_h_total']:.1f}")
            mix = (f"{g['n_ded']} выделенных" + (f" + burst {g['overflow_h_day']:.1f} ч/сут почасово"
                   if g['overflow_h_day'] > 0.01 else "")) if g['n_ded'] else \
                  f"почасовая аренда {g['gpu_h_total']:.1f} ч/сут"
            print(f"    GPU-закупка: {mix} = {g['cost_rub']:,.0f} ₽/мес")
            print(f"    Пиковый час: {pd['speech_hours']:.0f} ч речи → {pd['gpu_h_needed']:.2f} GPU-ч → "
                  f"парк разберёт за {pd['drain_min']:.0f} мин")
            print(f"    Хранение: +{m['storage']['gb_new']:.0f} ГБ/мес, steady {m['storage']['gb_steady']:.0f} ГБ "
                  f"→ {m['storage']['cost_rub']:,.0f} ₽; бэкенд {m['backend']:,.0f} ₽; LTE {m['lte']:,.0f} ₽")
            print(f"    (альтернатива LLM через GigaChat Pro batch: +{m['llm_api_alt_rub']:,.0f} ₽/мес)")
            print(f"    ИТОГО: {m['total_rub']:,.0f} ₽/мес = {m['per_mgr_rub']:,.0f} ₽ = "
                  f"${m['per_mgr_usd']:.2f}/менеджер (бюджет $15)")
        cx = capex(s)
        print(f"  CAPEX: бейдж ${cx['badge']:.0f} (+10% ЗИП = ${cx['badge_sp']:.0f}) + док ${cx['dock']:.0f} "
              f"+ шлюз ${cx['gw']:.1f} = ${cx['per_mgr']:.0f}/менеджер (бюджет $100); "
              f"парк ${cx['fleet']:,.0f}")
        print()


if __name__ == "__main__":
    report()
