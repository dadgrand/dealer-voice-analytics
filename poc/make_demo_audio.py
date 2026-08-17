# -*- coding: utf-8 -*-
"""Генерация демо-диалога «менеджер—клиент» для PoC.

Синтезирует реплики через Windows SAPI (офлайн), затем сводит их в двухканальный
WAV, моделирующий носимый бейдж менеджера:
  канал 0 («вверх», ко рту носителя): менеджер громкий, клиент тихий;
  канал 1 («фронт», на собеседника):  менеджер приглушён, клиент слышнее.
Именно эта асимметрия позволяет пайплайну разделять роли без голосовых профилей.

Запуск (только Windows): python make_demo_audio.py  ->  demo.wav
На других ОС используйте готовый demo.wav из репозитория.
"""
import base64
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 16_000
OUT = Path(__file__).parent / "demo.wav"

# (роль, текст, скорость SAPI −10..10)
DIALOGUE = [
    ("manager", "Добрый день! Меня зовут Андрей. Подбираете кроссовер? "
                "Кстати, у нас в зале ведётся аудиозапись для контроля качества.", 1),
    ("client",  "Здравствуйте. Вот эта комплектация — сколько стоит?", -2),
    ("manager", "Комфорт — два миллиона восемьсот. До конца месяца действует акция: "
                "зимняя резина и коврики в подарок.", 1),
    ("client",  "Дорого. В салоне на Волгоградке такую же дешевле предлагали.", -2),
    ("manager", "Понимаю. Давайте посчитаем с трейд-ином — вы сейчас на чём ездите?", 1),
    ("client",  "Королла двенадцатого года.", -2),
    ("manager", "За неё дадим порядка шестисот тысяч, тогда доплата заметно меньше. "
                "Запишу вас на тест-драйв в субботу на одиннадцать?", 1),
    ("client",  "Я подумаю.", -2),
    ("manager", "Тогда завтра пришлю расчёт по кредиту и трейд-ину. "
                "Оставите номер телефона?", 1),
    ("client",  "Восемь девятьсот шестнадцать, сто двадцать три, сорок пять, шестьдесят семь.", -2),
]

# усиление (амплитуда) источника в каналах [вверх, фронт]
GAINS = {"manager": (1.00, 0.50), "client": (0.18, 0.35)}
PAUSE_S = 0.55


def sapi_tts(text: str, rate: int, wav_path: Path) -> None:
    """Синтез фразы через SAPI из powershell.exe (кодировка через -EncodedCommand)."""
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$ru = $s.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Culture.Name -eq 'ru-RU' }} | Select-Object -First 1
if ($ru) {{ $s.SelectVoice($ru.VoiceInfo.Name) }}
$s.Rate = {rate}
$s.SetOutputToWaveFile('{wav_path}', (New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({SR},[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono)))
$s.Speak(@'
{text}
'@)
$s.Dispose()
"""
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", enc], check=True,
                   capture_output=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        pieces = []
        for i, (role, text, rate) in enumerate(DIALOGUE):
            f = Path(td) / f"{i:02d}.wav"
            sapi_tts(text, rate, f)
            audio, sr = sf.read(f, dtype="float32")
            assert sr == SR, f"SAPI вернул {sr} Гц"
            pieces.append((role, audio))

        total = sum(len(a) for _, a in pieces) + int(PAUSE_S * SR) * (len(pieces) + 1)
        mix = np.zeros((total, 2), dtype=np.float32)
        pos = int(PAUSE_S * SR)
        for role, audio in pieces:
            g_up, g_front = GAINS[role]
            mix[pos:pos + len(audio), 0] += audio * g_up
            mix[pos:pos + len(audio), 1] += audio * g_front
            pos += len(audio) + int(PAUSE_S * SR)

        rng = np.random.default_rng(7)
        mix += rng.normal(0, 0.004, mix.shape).astype(np.float32)  # «шум зала»
        mix = np.clip(mix / max(1.0, np.abs(mix).max()) * 0.9, -1, 1)
        sf.write(OUT, mix, SR, subtype="PCM_16")
        print(f"OK: {OUT} ({len(mix)/SR:.1f} c, {OUT.stat().st_size/1024:.0f} КБ)")


if __name__ == "__main__":
    main()
