프리미어 레지던스 푸꾸옥 클라우드 감시기 V2

1) ZIP 압축을 풉니다.
2) DEPLOY_CLOUD.bat 더블클릭합니다.
3) 최초 1회 GitHub 로그인/승인용 브라우저가 뜨면 로그인 후 승인합니다.
4) 나머지는 자동입니다.

기존 V4/V4 텔레그램 감시기의 config.json을 Downloads/Desktop/Documents에서 자동으로 찾습니다.
못 찾을 경우 기존 V4 폴더의 config.json을 이 폴더 안에 복사한 뒤 DEPLOY_CLOUD.bat을 다시 실행하면 됩니다.

배포 완료 후에는 PC를 꺼도 됩니다.
GitHub Actions가 약 5분 간격으로 확인하고 객실이 열리면 Telegram으로 알립니다.

주의: 저장소는 public으로 생성하지만 Telegram Bot Token/Chat ID는 GitHub Secrets에만 저장되고 공개 코드에는 들어가지 않습니다.
