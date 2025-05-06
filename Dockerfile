FROM python:slim

ARG SQLALCHEMY_DATABASE_URI=

WORKDIR /opt/transcription-app/
COPY . /opt/transcription-app/
RUN mkdir -p {uploads,instance}
RUN pip install --no-cache-dir -r requirements.txt
RUN python reset_db.py

VOLUME /opt/transcription-app/{uploads,instance}

EXPOSE 8899

# Запускаем приложение
CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:8899", "--timeout", "600", "app:app"]