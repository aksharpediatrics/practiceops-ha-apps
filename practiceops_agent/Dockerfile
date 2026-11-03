# Built on the Pi by Home Assistant when the App is installed or updated.
FROM python:3.12-alpine

# tzdata: office hours are New York time.
RUN apk add --no-cache tzdata

COPY agent.py /agent.py

CMD ["python", "-u", "/agent.py"]
