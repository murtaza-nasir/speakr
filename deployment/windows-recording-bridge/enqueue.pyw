"""Explorer SendTo entry. Persist first; acknowledge receipt, not completion."""
import ctypes
import html
import os
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from common import ROOT, config, enqueue, start_worker


def main():
    window = tk.Tk()
    window.withdraw()
    try:
        args = sys.argv[1:]
        if args and args[0] == '--':
            args = args[1:]
        cfg = config()
        state_root = Path(cfg.get('state_dir', ROOT))
        request_id, report = enqueue(args, state_root)
        report.parent.mkdir(parents=True, exist_ok=True)
        # The worker replaces this provisional, integration-owned report.
        if not report.exists():
            report.write_text('<!doctype html><meta charset="utf-8"><title>Расшифровка</title>'
                              '<h1>Запрос сохранён</h1><p>Подготовка очереди. Обновите страницу через минуту.</p>'
                              '<p>' + html.escape(request_id) + '</p>', encoding='utf-8')
        launch_error = None
        try:
            start_worker(cfg, ROOT)
        except OSError:
            launch_error = 'Запрос сохранён, но запуск обработчика не удался. Откройте инструкцию Speakr.'
        window.deiconify()
        window.title('Speakr — на расшифровку')
        window.geometry('640x250')
        frame = ttk.Frame(window, padding=22)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Запрос сохранён', font=('Segoe UI', 16)).pack(anchor='w')
        ttk.Label(frame, text=launch_error or f'Выбрано объектов: {len(args)}. Проверка и обработка в фоне.',
                  wraplength=590).pack(anchor='w', pady=(14, 8))
        ttk.Label(frame, text='Готовые расшифровки появятся в Obsidian.\nПодробности и причины пропусков — в отчёте запроса.',
                  wraplength=590).pack(anchor='w')
        ttk.Label(frame, text=str(report), wraplength=590, foreground='#555').pack(anchor='w', pady=10)
        buttons = ttk.Frame(frame)
        buttons.pack(anchor='e')
        ttk.Button(buttons, text='Открыть отчёт', command=lambda: os.startfile(report)).pack(side='left', padx=6)
        ttk.Button(buttons, text='Закрыть', command=window.destroy).pack(side='left')
        window.mainloop()
    except Exception as exc:
        messagebox.showerror('Speakr — запрос не сохранён',
                             'Не удалось сохранить запрос.\n' + type(exc).__name__ + ': ' + str(exc)[:220], parent=window)
        window.destroy()


if __name__ == '__main__':
    main()
