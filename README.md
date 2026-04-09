# vManage → Webex Alarm Notifier

Cisco SD-WAN vManage 알람을 실시간으로 감지해 Webex 공간으로 전달합니다.

## 동작 방식

```
vManage (REST API) ──polling──▶ notifier.py ──▶ Webex Bot API ──▶ Webex Space
```

- 지정한 주기(기본 60초)마다 vManage `/dataservice/alarms` API를 폴링
- 새 알람 발생 시 심각도별 이모지와 함께 Webex 메시지 전송
- 심각도 필터링 지원 (critical, major, medium, minor, warning)

## 알람 메시지 예시

```
🔴 [CRITICAL] vManage Alarm
- Type: BFD_NODE_DOWN
- Device: branch-router-01 (10.0.0.1)
- Time: 2026-04-09 22:30:00 UTC
- Message: BFD session to 10.0.0.2 is down
```

## 설치 및 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경 변수 설정
cp .env.example .env
vi .env   # 값 입력

# 실행
python vmanage_webex_notifier.py
```

## 환경 변수

| 변수 | 설명 | 기본값 |
|---|---|---|
| `VMANAGE_HOST` | vManage URL | `https://vmanage.example.com` |
| `VMANAGE_USER` | vManage 계정 | `admin` |
| `VMANAGE_PASS` | vManage 비밀번호 | - |
| `WEBEX_TOKEN` | Webex Bot 토큰 | - |
| `WEBEX_ROOM_ID` | 알람 전송할 Webex Room ID | - |
| `POLL_INTERVAL` | 폴링 주기 (초) | `60` |
| `SEVERITY_FILTER` | 심각도 필터 (쉼표 구분, 빈값=전체) | `critical,major` |

## Webex Bot 설정

1. [Webex Developer](https://developer.webex.com/my-apps) → **Create a Bot**
2. Bot Token 복사 → `.env`의 `WEBEX_TOKEN`에 입력
3. Bot을 알람 받을 Webex Space에 초대
4. Space의 Room ID 확인:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" \
     https://webexapis.com/v1/rooms | python3 -m json.tool
   ```
5. `WEBEX_ROOM_ID`에 입력

## Docker로 실행

```bash
docker build -t vmanage-webex .
docker run -d --env-file .env vmanage-webex
```
