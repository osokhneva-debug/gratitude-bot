#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dry-run скрипт для переименования файлов:
1. Замена пробелов на подчёркивания
2. Приведение расширений к нижнему регистру
"""

import os
from pathlib import Path

def process_filename(filename):
    """
    Обрабатывает имя файла:
    - Заменяет пробелы на подчёркивания
    - Приводит расширение к нижнему регистру
    """
    # Разделяем имя и расширение
    if '.' in filename:
        name, ext = filename.rsplit('.', 1)
        # Заменяем пробелы на подчёркивания в имени
        new_name = name.replace(' ', '_')
        # Приводим расширение к нижнему регистру
        new_ext = ext.lower()
        return f"{new_name}.{new_ext}"
    else:
        # Если нет расширения, просто заменяем пробелы
        return filename.replace(' ', '_')

def main():
    # Получаем текущую директорию
    base_dir = Path(__file__).parent
    actions = []
    
    # Проходим по всем файлам в директории
    for item in base_dir.iterdir():
        if item.is_file() and item.name != 'rename_dryrun.py' and item.name != 'actions.log':
            old_name = item.name
            new_name = process_filename(old_name)
            
            if old_name != new_name:
                actions.append((old_name, new_name))
    
    # Сортируем для удобства
    actions.sort()
    
    # Выводим список переименований
    print("=" * 60)
    print("DRY-RUN: Список переименований")
    print("=" * 60)
    print()
    
    if not actions:
        print("Нет файлов для переименования.")
    else:
        for old, new in actions:
            print(f"  {old}")
            print(f"  → {new}")
            print()
    
    # Записываем в actions.log
    log_path = base_dir / 'actions.log'
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write("Список переименований (dry-run):\n")
        f.write("=" * 60 + "\n\n")
        if not actions:
            f.write("Нет файлов для переименования.\n")
        else:
            for old, new in actions:
                f.write(f"{old} → {new}\n")
    
    print(f"Список сохранён в: {log_path}")
    print()
    print("Для применения изменений введите 'Применить'")

if __name__ == '__main__':
    main()




