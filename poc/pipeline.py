# -*- coding: utf-8 -*-
"""PoC пайплайна этапа 1: WAV бейджа -> VAD -> роли -> ASR -> JSON-разбор.

Демонстрирует ключевую идею архитектуры: разделение «менеджер/клиент» по
асимметрии каналов носимого бейджа (канал 0 — «вверх», ко рту носителя;
канал 1 — «фронт», на собеседника). Голосовые профили не строятся — система
остаётся вне режима биометрии (ст. 11 152-ФЗ).

Запуск:  python pipeline.py demo.wav [--out analysis.json]
Выход:   транскрипт с ролями (stdout) + analysis.json по схеме приложения В.

ASR — GigaAM v2 CTC (SberDevices, MIT): актуальный pip-релиз; в проде — v3.
Первый запуск скачивает веса (~0.9 ГБ) в ~/.cache/gigaam.
LLM-разбор: правило-based заглушка; с переменной окружения GIGACHAT_API_KEY
и флагом --llm gigachat — реальный вызов GigaChat (структура выхода та же).
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Машины с системным (реестровым) SOCKS-прокси вешают большие загрузки;
# если явного прокси в окружении нет — ходим напрямую. В обычных средах — noop.
if not any(os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")):
    os.environ.setdefault("NO_PROXY", "*")

import numpy as np
import soundfile as sf

SR = 16_000
FRAME = int(0.02 * SR)          # кадр оценки ролей: 20 мс
RATIO_DB_MANAGER = 2.0          # канал «вверх» громче фронта минимум на 2 дБ -> менеджер
MEDIAN_WIN = 9                  # медианное сглаживание решений (~180 мс)
MERGE_GAP_S = 0.6               # склейка соседних кусков одной роли


@dataclass
class Turn:
    role: str      # manager | client
    t0: float
    t1: float
    text: str = ""


def read_badge_wav(path: Path) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] != 2:
        sys.exit("Ожидается двухканальный WAV бейджа (канал 0 — вверх, 1 — фронт)")
    if sr != SR:
        sys.exit(f"Ожидается {SR} Гц, получено {sr}")
    return audio


def vad_segments(mono: np.ndarray) -> list[tuple[float, float]]:
    from silero_vad import load_silero_vad, get_speech_timestamps
    model = load_silero_vad(onnx=True)
    ts = get_speech_timestamps(mono, model, sampling_rate=SR,
                               return_seconds=True, speech_pad_ms=120)
    return [(t["start"], t["end"]) for t in ts]


def frame_roles(audio: np.ndarray) -> np.ndarray:
    """Решение «кто говорит» на кадр 20 мс по разнице энергий каналов (дБ)."""
    n = len(audio) // FRAME
    x = audio[: n * FRAME].reshape(n, FRAME, 2)
    rms = np.sqrt((x ** 2).mean(axis=1) + 1e-9)          # (n, 2)
    ratio_db = 20 * np.log10(rms[:, 0] / rms[:, 1])
    roles = (ratio_db > RATIO_DB_MANAGER).astype(int)     # 1 = менеджер
    # медианное сглаживание против дрожания на фрикативах
    pad = MEDIAN_WIN // 2
    padded = np.pad(roles, pad, mode="edge")
    return np.array([np.median(padded[i:i + MEDIAN_WIN]) for i in range(n)])


def build_turns(audio: np.ndarray, segments: list[tuple[float, float]]) -> list[Turn]:
    roles = frame_roles(audio)
    turns: list[Turn] = []
    for seg_t0, seg_t1 in segments:
        f0, f1 = int(seg_t0 * SR / FRAME), min(int(seg_t1 * SR / FRAME), len(roles))
        if f1 <= f0:
            continue
        cur = None
        for f in range(f0, f1):
            role = "manager" if roles[f] else "client"
            if cur is None or cur.role != role:
                if cur:
                    turns.append(cur)
                cur = Turn(role, f * FRAME / SR, (f + 1) * FRAME / SR)
            else:
                cur.t1 = (f + 1) * FRAME / SR
        if cur:
            turns.append(cur)
    # склейка коротких разрывов одной роли; выброс осколков < 0.3 с
    merged: list[Turn] = []
    for t in turns:
        if merged and merged[-1].role == t.role and t.t0 - merged[-1].t1 < MERGE_GAP_S:
            merged[-1].t1 = t.t1
        else:
            merged.append(t)
    return [t for t in merged if t.t1 - t.t0 >= 0.3]


def transcribe(audio: np.ndarray, turns: list[Turn], model_name: str) -> None:
    """ASR каждой реплики. Кормим GigaAM тензором напрямую (минуя file-API,
    которое требует системный ffmpeg); формат совпадает с их load_audio:
    float32, 16 кГц, моно, [-1..1]. Версия пакета закреплена в requirements."""
    import gigaam
    import torch
    model = gigaam.load_model(model_name)
    mono = audio.mean(axis=1)
    with torch.inference_mode():
        for t in turns:
            piece = mono[int(t.t0 * SR): int(t.t1 * SR)]
            wav = torch.from_numpy(np.ascontiguousarray(piece))
            wav = wav.to(model._device).to(model._dtype).unsqueeze(0)
            length = torch.full([1], wav.shape[-1], device=model._device)
            encoded, encoded_len = model.forward(wav, length)
            t.text = model.decoding.decode(model.head, encoded, encoded_len)[0].strip()


# --- разбор: одна и та же JSON-схема из правил или из GigaChat ---------------

OBJECTION_MARKERS = {
    "price": ["дорого", "дороже", "не потяну", "дешевле предлагали"],
    "competitor": ["в салоне на", "у дилера", "в другом салоне", "предлагали"],
    "timing": ["подумаю", "не сейчас", "позже"],
}
CHECKLIST = [
    ("уведомил о записи разговора", ["ведется аудиозапись", "запись разговора"]),
    ("выяснил текущий автомобиль (трейд-ин)", ["на чем ездите", "трейд ин", "трейдин"]),
    ("предложил тест-драйв", ["тест драйв"]),
    ("взял контакт", ["номер телефона", "оставите номер"]),
    ("зафиксировал следующий шаг", ["пришлю расчет", "наберу вас", "завтра"]),
]


def _norm(s: str) -> str:
    """Нормализация для сопоставления: регистр, ё->е, дефисы -> пробелы."""
    return s.lower().replace("ё", "е").replace("-", " ")


def analyze_rules(turns: list[Turn]) -> dict:
    mtext = _norm(" ".join(t.text for t in turns if t.role == "manager"))
    objections = []
    for t in turns:
        if t.role != "client":
            continue
        low = _norm(t.text)
        for typ, markers in OBJECTION_MARKERS.items():
            if any(_norm(m) in low for m in markers):
                # отработано = менеджер содержательно ответил в течение 20 секунд
                handled = any(x.role == "manager" and 0 < x.t0 - t.t1 < 20
                              and len(x.text.split()) >= 3 for x in turns)
                objections.append({"type": typ, "quote_t0": round(t.t0, 1),
                                   "text": t.text, "handled": handled})
    checklist = [{"item": item, "done": any(_norm(m) in mtext for m in markers)}
                 for item, markers in CHECKLIST]
    return {
        "schema_version": "1.3-poc",
        "summary": "PoC-разбор на правилах: см. objections/checklist; "
                   "в проде этот блок формирует LLM (T-Pro 2.0 / GigaChat).",
        "outcome": "thinking" if any(o["type"] == "timing" for o in objections)
                   else "next_step_agreed",
        "objections": objections,
        "checklist": checklist,
        "entities_for_matching": _entities(turns),
    }


def _entities(turns: list[Turn]) -> dict:
    client = _norm(" ".join(t.text for t in turns if t.role == "client"))
    ent = {}
    if any(w in client for w in ("королла", "каролла", "каралла", "corolla")):
        ent["trade_in_model"] = "Toyota Corolla"
    if "восемь девятьсот" in client:
        ent["phone_mentioned"] = True
    return ent


def analyze_gigachat(turns: list[Turn]) -> dict:
    """Реальный LLM-разбор через GigaChat API (нужен GIGACHAT_API_KEY)."""
    import base64
    import uuid
    import urllib.request

    key = os.environ["GIGACHAT_API_KEY"]
    tok_req = urllib.request.Request(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        data=b"scope=GIGACHAT_API_PERS",
        headers={"Authorization": f"Basic {key}",
                 "RqUID": str(uuid.uuid4()),
                 "Content-Type": "application/x-www-form-urlencoded"})
    token = json.load(urllib.request.urlopen(tok_req))["access_token"]

    transcript = "\n".join(f"[{t.role} {t.t0:.1f}s] {t.text}" for t in turns)
    prompt = ("Разбери разговор менеджера автосалона с клиентом. Верни строго JSON "
              "с полями: summary, outcome, objections[{type,quote_t0,text,handled}], "
              "checklist[{item,done}], entities_for_matching.\n\n" + transcript)
    req = urllib.request.Request(
        "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
        data=json.dumps({"model": "GigaChat-2", "messages": [
            {"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    answer = json.load(urllib.request.urlopen(req))
    return json.loads(answer["choices"][0]["message"]["content"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wav", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "analysis.json")
    ap.add_argument("--model", default="v2_ctc", help="модель GigaAM (v2_ctc | v2_rnnt)")
    ap.add_argument("--llm", choices=["rules", "gigachat"], default="rules")
    args = ap.parse_args()

    audio = read_badge_wav(args.wav)
    print(f"Аудио: {len(audio)/SR:.1f} c, 2 канала")

    segments = vad_segments(audio.mean(axis=1))
    print(f"VAD: {len(segments)} речевых сегментов, "
          f"{sum(b-a for a, b in segments):.1f} c речи")

    turns = build_turns(audio, segments)
    print(f"Роли по асимметрии каналов: {len(turns)} реплик\n")

    transcribe(audio, turns, args.model)
    turns = [t for t in turns if t.text]   # осколки ролевого дрожания без речи
    for t in turns:
        who = "МЕНЕДЖЕР" if t.role == "manager" else "КЛИЕНТ  "
        print(f"  [{t.t0:6.1f}–{t.t1:6.1f}] {who} {t.text}")

    analysis = analyze_gigachat(turns) if args.llm == "gigachat" else analyze_rules(turns)
    analysis["transcript"] = [
        {"t0": round(t.t0, 2), "t1": round(t.t1, 2), "role": t.role, "text": t.text}
        for t in turns]
    args.out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\nРазбор: {args.out}")
    print(json.dumps({k: v for k, v in analysis.items() if k != "transcript"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
