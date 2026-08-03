# save_visiogetbb
Архив форума

# Использование
```
# Полное архивирование (займёт много времени)
python parser.py -o forum_archive

# С ограничением количества страниц и задержкой между HTTP-запросами
python parser.py -o forum_archive --max-pages 100 --delay 0.1

# Начать с конкретного раздела
python parser.py -o forum_archive --start-url "https://visio.getbb.ru/viewforum.php?f=3"

# Подробный лог
python parser.py -o forum_archive -v
```
Подробности [здесь](https://github.com/Surrogate-TM/save_visiogetbb/pull/2)
