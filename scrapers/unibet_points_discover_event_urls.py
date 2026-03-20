name: unibet-event-points-from-hub

on:
  workflow_dispatch:
    inputs:
      hub_url:
        description: "URL hub Unibet NHL"
        required: true
        default: "https://www.unibet.fr/sport/hockey-sur-glace/etats-unis/nhl?filter=R%C3%A9sultat&subFilter=R%C3%A9sultat+du+match"
        type: string
      headless:
        description: "Lancer en headless"
        required: true
        default: true
        type: boolean

jobs:
  run-batch-from-hub:
    runs-on: [self-hosted, macOS, X64, morgan-runner]
    timeout-minutes: 120

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Debug Python
        run: |
          which python3 || true
          python3 --version || true
          which pip3 || true
          pip3 --version || true
          pwd
          ls -la

      - name: Install dependencies
        run: |
          python3 -m pip install --upgrade pip
          pip3 install -r requirements.txt
          python3 -m playwright install chromium

      - name: Discover event URLs from hub
        env:
          UNIBET_HUB_URL: ${{ github.event.inputs.hub_url }}
          PW_HEADLESS: ${{ github.event.inputs.headless }}
        run: |
          python3 scrapers/unibet_points_discover_event_urls.py

      - name: Export discovered event URLs
        run: |
          LATEST_DISCOVERY_DIR=$(ls -td artifacts/unibet_points_discover_event_urls/* | head -n 1)
          export DISCOVERY_JSON_PATH="$LATEST_DISCOVERY_DIR/discovered_event_urls.json"

          python3 - <<'PY'
          import json
          import os
          from pathlib import Path

          path = Path(os.environ["DISCOVERY_JSON_PATH"])
          payload = json.loads(path.read_text(encoding="utf-8"))
          urls = payload.get("event_urls", [])

          if not urls:
              raise SystemExit("Aucune event URL découverte depuis le hub Unibet.")

          csv_urls = ",".join(urls)

          github_env = os.environ["GITHUB_ENV"]
          with open(github_env, "a", encoding="utf-8") as f:
              f.write(f"UNIBET_EVENT_URLS={csv_urls}\n")

          print(f"Discovered URLs exported to GITHUB_ENV: {len(urls)}")
          PY

      - name: Run points batch
        env:
          UNIBET_EVENT_URLS: ${{ env.UNIBET_EVENT_URLS }}
          PW_HEADLESS: ${{ github.event.inputs.headless }}
        run: |
          python3 scrapers/unibet_event_points_batch_runner.py

      - name: Build acceptance report
        run: |
          LATEST_BATCH_DIR=$(ls -td artifacts/unibet_event_points_batch_runner/* | head -n 1)
          export BATCH_SUMMARY_PATH="$LATEST_BATCH_DIR/batch_summary.json"
          python3 scrapers/unibet_event_points_acceptance_report.py

      - name: Build normalized points json
        run: |
          LATEST_BATCH_DIR=$(ls -td artifacts/unibet_event_points_batch_runner/* | head -n 1)
          export ACCEPTANCE_REPORT_PATH="$LATEST_BATCH_DIR/acceptance_report.json"
          python3 scrapers/unibet_event_points_normalize_accepted.py

      - name: Upload artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: unibet-event-points-from-hub
          path: artifacts/
          retention-days: 7
