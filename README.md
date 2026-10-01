# ym-alert-proxy

Sends Prometheus Alertmanager alerts to Yandex Messenger chats.

Alertmanager can't talk to Yandex Messenger, so this sits in between: it takes the regular Alertmanager webhook, looks at the `team` label of each alert and posts a message to that team's chat via the [Bot API](https://yandex.ru/dev/messenger/doc/ru/).

A message looks like this:

```
🔴 FIRING
Alert: HighErrorRate
Severity: CRITICAL
Team: backend
Env: prod

Summary: 5xx rate is above 5%
Description: Current value is 7.300%

Runbook: https://wiki.example.com/runbooks/high-error-rate
Dashboard: https://grafana.example.com/d/abc?var-namespace=shop

Silence: https://alertmanager.example.com/#/silences/new?filter=%7Balertname%3D%22HighErrorRate%22%7D
```

The silence link opens the "new silence" form in Alertmanager with the alert's labels already filled in. If the alert has a `namespace` label, it is added to the Grafana link as `var-namespace`.

## Running

You need a bot in Yandex Messenger (its OAuth token) and the bot has to be a member of the chats it posts to.

```bash
cp .env.example .env
docker build -t ym-alert-proxy .
docker run --rm -p 5000:5000 --env-file .env ym-alert-proxy
```

Test it without Alertmanager:

```bash
curl -X POST http://localhost:5000/alert \
  -H "Authorization: Bearer $WEBHOOK_TOKEN" \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"Test","severity":"warning","team":"backend"},"annotations":{"summary":"Test alert"}}]}'
```

## Settings

Everything is set through environment variables.

| Variable | Description |
|---|---|
| `YANDEX_TOKEN` | Bot OAuth token. Required, the service won't start without it. |
| `<TEAM>_CHAT_ID` | Chat for a team, several chats can be listed with commas. The name is the `team` label in upper case, anything that isn't a letter or digit becomes `_`: `data-platform` gives `DATA_PLATFORM_CHAT_ID`. |
| `WEBHOOK_TOKEN` | If set, `/alert` only accepts requests with `Authorization: Bearer <token>`. |
| `ALERTMANAGER_BASE_URL` | Alertmanager UI address for silence links. No variable, no link. |
| `YANDEX_URL` | Bot API endpoint, if you need to override it. |
| `LOG_LEVEL` | `INFO` by default. |

An alert may list several teams in the label (`team: backend,infra`). Alerts with no `team` label or with a team that has no chat configured are dropped, with a warning in the log.

## Alertmanager

```yaml
receivers:
  - name: yandex-messenger
    webhook_configs:
      - url: http://ym-alert-proxy:5000/alert
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials: <WEBHOOK_TOKEN>
```

The proxy reads these fields from an alert:

- labels: `team`, `alertname`, `severity`, `component`, `env`, `namespace` (or `exported_namespace`)
- annotations: `summary`, `description`, `runbook`, `grafana_url` (or `grafana_dashboard`), `silence_url` to replace the generated link

```yaml
- alert: HighErrorRate
  expr: sum(rate(http_requests_total{code=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) > 0.05
  labels:
    severity: critical
    team: backend
  annotations:
    summary: 5xx rate is above 5%
    runbook: https://wiki.example.com/runbooks/high-error-rate
    grafana_url: https://grafana.example.com/d/abc
```

## Things to know

`/alert` answers `200` when all messages went out, `207` when only some did and `500` when none did. Alertmanager retries on `500` only, so with `207` the failed part is lost (it is in the log). Requests to the Bot API are retried three times on `429` and `5xx` before giving up.

There is no state, so run as many replicas as you like. `GET /health` is there for probes. An example Deployment is in [deploy/kubernetes.yaml](deploy/kubernetes.yaml).

Multi-arch image:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/ym-alert-proxy:<tag> --push .
```

Tests: `pip install -r requirements.txt pytest && pytest`

## License

MIT
