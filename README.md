# Job alerts: talent / recruiting roles, remote, worldwide, £70k+

Pings your phone within minutes of a matching role appearing on a company career board or remote job board.

## 10-minute setup (phone notifications, free)

1. **Phone:** install the free **ntfy** app (iOS/Android). Subscribe to a topic with a long random name,
   e.g. `paushali-jobs-k39x82qv`. The name is effectively your password, so don't share it.
2. **GitHub:** create a new repo (use **public** for unlimited free Actions minutes; the repo only holds job
   links, your topic name stays in Secrets). Upload everything in this folder, including the `.github` folder.
3. Repo -> Settings -> Secrets and variables -> Actions -> New secret:
   `NTFY_TOPIC` = your topic name.
   (Optional Telegram: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.)
4. Repo -> Actions tab -> enable workflows -> run **job-alerts** once manually.
   First run sends up to 20 currently-open matches, then only new ones.

## Tuning
Everything is in `config.py`: titles, salary floor, how to treat unlisted salary, and the company boards list.
Add companies by their careers-page slug (boards.greenhouse.io/**slug**, jobs.lever.co/**slug**, jobs.ashbyhq.com/**slug**).

## Alert colours
- 🟢 worldwide + listed salary over your floor
- 🔵 worldwide, salary not listed
- 🟡 plain "Remote" with no country stated: check eligibility before getting excited

## Run it on your own machine instead
```
pip install -r requirements.txt
export NTFY_TOPIC=your-topic
python notifier.py --test          # test push
python notifier.py --loop 120      # poll every 2 minutes
```
