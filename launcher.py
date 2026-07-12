"""
Bitta Railway service'da 2 botni birga yurgizadi (umumiy /data papka).
  Asosiy bot:  python bot.py
  Moliya boti: python finance_bot.py
  Sayt:        python web.py  (PORT ni Railway beradi)
Ikkalasi /data ni baham ko'radi -> moliya boti jonli fayllarni real-time o'qiydi.

Railway "Start Command": python launcher.py
"""
import subprocess, sys, time

PROCS = []

def start(name, *args):
    print(f"[launcher] {name} ishga tushyapti: {' '.join(args)}", flush=True)
    p = subprocess.Popen([sys.executable, *args])
    PROCS.append((name, p))

def stop_all():
    for name, p in PROCS:
        if p.poll() is None:
            print(f"[launcher] {name} to'xtatilyapti", flush=True)
            p.terminate()

if __name__ == "__main__":
    start("asosiy-bot", "bot.py")
    start("moliya-bot", "finance_bot.py")
    start("sayt", "web.py")
    try:
        while True:
            for name, p in PROCS:
                ret = p.poll()
                if ret is not None:
                    print(f"[launcher] {name} to'xtadi (kod {ret}). Qayta tiklash uchun chiqamiz.", flush=True)
                    stop_all()
                    sys.exit(1)   # Railway service'ni qayta ishga tushiradi
            time.sleep(5)
    except KeyboardInterrupt:
        stop_all()
