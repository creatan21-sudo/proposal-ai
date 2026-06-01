# railway.json startCommand 우선 적용됨 (workers=1 threads=16)
# 이 파일은 로컬/Heroku 호환용 참고값입니다.
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 16 --timeout 600 --max-requests 100 --max-requests-jitter 20
