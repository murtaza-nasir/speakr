# Восстановление Speakr Recording Bridge

Кандидат для локальной Windows-установки Speakr. Готовый текст публикуется в Obsidian при подтверждённом сбое резюме; резюме повторяется отдельно через API, без ASR. Сторож запускает остановленные компоненты после входа пользователя и проверяет их раз в минуту.

## Что сохраняется

- Существующие baseline, дедупликация, provenance, aliases, ручные speaker edits и публикации.
- С 14.10.2026 новая публикация обновляет одну заметку по закреплённому пути; раздел `## Мои заметки` сохраняется. Перед заменой старые байты копируются вне vault в `automation/backups/note-revisions`.
- Удалённые пользователем заметки не восстанавливаются автоматически.
- Старые failed jobs после установки заморожены до отдельного разрешения на backlog.
- Числовой recording ID проверяется по исходному bridge filename перед публикацией/повтором.

Отказ неизвестного этапа, неоднозначный POST, потерянная история jobs, изменённый transcript или существующие календарные события требуют проверки человеком. Текущий сервер очищает события при повторе summary, поэтому автоматический повтор записей с событиями запрещён. Отключение auto-summary пользователем/администратором соблюдается.

## Одна заметка на запись и старые версии

При изменении текста, говорящих, provenance или резюме обработчик атомарно заменяет сгенерированную часть существующей заметки. Ссылка и путь остаются прежними, пользовательский хвост от `## Мои заметки` сохраняется. Ручные изменения в сгенерированной части будут заменены; копия прежних байтов хранится вне Obsidian. Чужой `bridge_id`, неоднозначный пользовательский блок и вручную удалённая текущая заметка блокируют замену.

Для старых файлов `— версия ...` сначала выполните read-only preview:

```powershell
C:\Python314\python.exe deployment/windows-recording-bridge/single_note_migration.py --preview --root C:\Programs\Speakr\automation
```

Перед `--apply` установите maintenance pause, дождитесь завершения работающего bridge и сверьте preview. Миграция сохраняет SQLite и все затрагиваемые заметки в `C:\Programs\Speakr\automation\backups\single-note-<timestamp>`, оставляет один файл на каждый однозначный `bridge_id` и сообщает причины пропусков. При сбое продолжайте по тому же backup через `--resume <backup-path>`; не восстанавливайте старый SQLite поверх новых заданий. Отсутствующая последняя версия и конфликт пользовательских секций не разрешаются угадыванием.

## Проверка кандидата

```powershell
C:\Python314\python.exe -m unittest discover -s deployment/windows-recording-bridge/tests -v
powershell.exe -NoProfile -File deployment/windows-recording-bridge/install-recovery.ps1 -DryRun
powershell.exe -NoProfile -File deployment/windows-recording-bridge/tests/validate-candidate.ps1
```

Последняя команда создаёт отдельные задачи с уникальными именами и пустую временную очередь. Она проверяет установку, maintenance, heartbeat, восстановление остановленного candidate worker и rollback; удаляет только свои scheduled tasks, сохраняет evidence во временном каталоге. Она не переключает live-пайплайн и не перезагружает компьютер.

## Установка в рабочую систему

Требует отдельного разрешения на rollout после рассмотрения candidate evidence. `baseline-manifest.json` содержит SHA-256 действующих модулей на момент подготовки: изменение live source прекращает установку. `.env` и credentials не копируются в репозиторий.

```powershell
powershell.exe -NoProfile -File deployment/windows-recording-bridge/install-recovery.ps1 -Apply
```

Установщик сохраняет исходные файлы, config, XML и состояние задач, maintenance и Docker AutoStart. Через graceful stop ждёт worker; занятый worker принудительно не убивает. SQLite backup API сохраняет очередь с integrity check. Docker AutoStart включается, existing bridge и Ollama tasks сохраняются, добавляется `Speakr Recovery Watchdog`. Результат `installed_paused`: активация отдельным шагом после read-back.

```powershell
powershell.exe -NoProfile -File C:\Programs\Speakr\automation\recovery-control.ps1 -Action Status
powershell.exe -NoProfile -File C:\Programs\Speakr\automation\recovery-control.ps1 -Action Resume
```

Resume не включает Disabled tasks и не снимает discovery pause. Backlog остаётся заморожен. Разрешить только проверенные job IDs после отдельного preview/approval:

```powershell
powershell.exe -NoProfile -File C:\Programs\Speakr\automation\recovery-control.ps1 -Action ApproveBacklog -JobIds <verified-id>
```

При maintenance worker не начинает новый upload/summary/export, сторож не запускает сервисы. In-flight запрос/atomic file write безопасно завершается; heartbeat показывает `quiesced`. `pause_watch` отдельно выключает автоматическое обнаружение файлов, позволяя завершить прежние и ручные jobs.

## Сторож и статус

`watchdog-status.json` доступен даже без bridge. `status.json` сохраняет старые ключи и добавляет этапы, возраст ожидания, attention и последнюю публикацию. `heartbeat.json` обновляется независимо от длинного ffmpeg/API цикла. Порт обычного Ollama `11434` не контролируется.

Один runtime action за проход, cooldown 120 секунд, не более трёх запусков компонента за 15 минут. Состояние хранится на диске. Запущенный Ollama/ASR с плохим health и неизвестной активной работой помечается degraded: GPU-задачи вслепую не убиваются. Сторож может остановить зависший bridge только после graceful stop, мёртвого heartbeat, истёкшего deadline и совпадения PID/start time/command path.

Локальное уведомление о длительном сбое появляется один раз через 10 минут, затем одно о восстановлении; оно не содержит текста или имени записи. Доставка/видимость зависит от Windows notification settings. Нет внешних сообщений.

## Откат

```powershell
powershell.exe -NoProfile -File deployment/windows-recording-bridge/install-recovery.ps1 -Rollback -BackupPath <returned-backup>
```

Откат сохраняет текущую очередь и новые заметки, восстанавливает исходные task states и discovery pause. Ранее Ready/Disabled задачи не запускаются. Drift установленного файла либо Docker AutoStart прекращает перезапись. Файлы и config переключаются атомарно после проверки SHA-256 временной копии. При частичной ошибке журнал установки автоматически возвращает прежние файлы, задачи, maintenance и stop marker; чужой drift прекращает откат до перезаписи. Старая SQLite не восстанавливается поверх новой истории.

Реальная перезагрузка и логин — отдельная проверка после rollout, с сохранением пользовательской работы. До неё нельзя утверждать, что установленная цепочка проверена после reboot.

## Проверка переносимости

Конфигурация содержит только примеры. Перед применением installer подготовьте baseline-manifest.json по SHA-256 текущих файлов целевой установки: встроенный manifest является историческим примером, а несовпадение блокирует установку. Не отключайте проверку drift. Задайте собственные пути vault, runtime image/config и имена задач. Проверяйте candidate до переключения рабочей системы. Локальные отчёты, реальные записи и пользовательские конфигурации в пакет не входят.
