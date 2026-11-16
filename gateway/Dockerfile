FROM python:3.13-alpine
# The guard at door 1 is one readable file. See gateway.py.
COPY gateway.py /gateway.py
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/gateway.py"]
