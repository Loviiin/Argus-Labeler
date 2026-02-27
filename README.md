# Argus Labeler

Aplicação web para revisão e rerotulagem colaborativa de captchas de rotação.

## Arquitetura atual (migrada)

- Backend FastAPI em `main.py`
- Frontend estático em `index.html`
- Persistência em **Supabase**:
  - Bucket público `captcha-images` (imagens + labels JSON)
  - Tabela `reviews` (reviews persistentes)
- Upload inicial de dataset via `upload_dataset.py`

## Dependências

```bash
pip install -r requirements.txt
```

## Variáveis de ambiente

Backend (`main.py`) exige:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SECRET_KEY`
- `FRONTEND_URL`
- `CLAIM_TTL_SECONDS` (opcional, padrão `300`)

Exemplo PowerShell:

```powershell
$env:SUPABASE_URL="https://SEU-PROJETO.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY="SEU_SERVICE_ROLE_KEY"
$env:SECRET_KEY="sua-chave-forte"
$env:FRONTEND_URL="http://127.0.0.1:5500"
```

Também pode usar arquivo `.env` local (carregado automaticamente pelo backend via `python-dotenv`):

1. Copie `.env.example` para `.env`
2. Preencha os valores reais

```powershell
Copy-Item .env.example .env
```

## Setup no Supabase

1. Crie bucket **público** chamado `captcha-images`.
2. Crie tabela `reviews`:

```sql
create table if not exists public.reviews (
  id text primary key,
  angle double precision not null,
  angle_original double precision not null,
  action text not null check (action in ('confirmed', 'adjusted', 'skipped')),
  raw_pixels jsonb null,
  slidebar_width integer null,
  icon_width integer null,
  timestamp timestamptz not null,
  labeled_by text not null
);
```

## Upload inicial para o bucket

Com as variáveis de ambiente definidas, execute:

```bash
python upload_dataset.py
```

O script:

- lê arquivos de `images/` (`*_inner.jpg`, `*_outer.jpg`)
- lê labels de `dataset/` (`*_label.json`)
- envia para `captcha-images`
- mostra progresso (`tqdm`)
- pula arquivos que já existem no bucket

## Rodar localmente

1. Inicie backend:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

2. Abra o frontend (`index.html`) com servidor estático e use:

```text
http://127.0.0.1:5500/index.html?key=sua-chave-forte
```

## Endpoints

- `GET /samples` → lista `{timestamp, angle_original, reviewed}`
- `GET /next-sample?reviewer_id=...` → reserva amostra colaborativa
- `GET /image/{filename}` → redireciona (`302`) para URL pública do Supabase
- `GET /label/{timestamp}` → baixa e parseia `{timestamp}_label.json` do bucket
- `POST /review` → upsert na tabela `reviews`
- `GET /export` → download JSON com todos os reviews persistidos
- `GET /progress` → `{total, confirmed, adjusted, skipped, remaining}`
- `GET /swagger` → documentação Swagger
- `GET /healthz` → healthcheck rápido para Render
- `GET /healthz/deep` → healthcheck com checagem de Supabase

Todas as rotas de negócio validam `?key=` com `SECRET_KEY`.

## Deploy

### Render (backend)

- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Variáveis de ambiente: as listadas acima (`SUPABASE_*`, `SECRET_KEY`, `FRONTEND_URL`)
- Health Check Path recomendado: `/healthz`

### Vercel (frontend)

- Publique o projeto estático
- O arquivo `vercel.json` já força deploy estático de `index.html` (evita erro `FUNCTION_INVOCATION_FAILED`)
- Em `index.html`, ajuste:

```js
const BACKEND_URL = "https://seu-backend.onrender.com";
```

- Acesse com chave:

```text
https://seu-frontend.vercel.app/?key=sua-chave-forte
```

## Persistência

Agora o `/export` é persistente porque lê da tabela `reviews` no Supabase. Reinício do backend não perde os reviews.
