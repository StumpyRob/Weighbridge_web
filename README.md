# weighbridge_web

FastAPI starter with SQLAlchemy, Alembic, and Postgres.

## Local setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Set environment variables:

```bash
set DATABASE_URL=postgresql+psycopg://weighbridge:weighbridge@localhost:5432/weighbridge
set SECRET_KEY=change-me
```

Run database migrations (only create a new revision when models change):

```bash
alembic upgrade head
```

If you are enabling required product units, seed lookup units first:

```bash
alembic upgrade 0a9b3d5c7e21
python -m app.seed
alembic upgrade head
```

Start the app:

```bash
uvicorn app.main:app --reload
```

## Docker

```bash
docker compose up --build
```

App: http://localhost:8000  
Health: http://localhost:8000/health
