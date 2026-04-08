down:
  rsync -avz --include="zfnbot/plugins/kisaragirin_onebot/config.py" --filter=":- .gitignore" --exclude=".git" zfn@65.49.212.5:/home/zfn/bot_renew/ ./

up:
  rsync -avz --include="zfnbot/plugins/kisaragirin_onebot/config.py" --include "test.py" --include "*.sh" --include=".env" --filter=":- .gitignore" --exclude=".git" ./ zfn@65.49.212.5:/home/zfn/bot_renew
