# Bocado

App de calorías: meta diaria según edad, peso, estatura y actividad (Mifflin-St Jeor) y cálculo de calorías a partir de fotos de platos con la API de Claude.

**Stack:** Flask · PostgreSQL · Claude API · un único `static/index.html` (español, inglés y japonés).

## Cómo funciona el acceso

- **La primera cuenta que se registra es la de administrador** (tú), con fotos sin límite. Regístrate nada más desplegar.
- Después, solo se puede registrar quien tenga el **código de invitación** (`SIGNUP_CODE`). Si dejas la variable vacía, el registro queda cerrado.
- Los demás usuarios tienen un máximo de `DAILY_PHOTO_LIMIT` fotos al día. Añadir comidas a mano no tiene límite.
- Cada usuario solo ve sus propios datos.

## Desplegar en Railway

1. Sube esta carpeta a un repositorio nuevo de GitHub (por ejemplo, `bocado`).
2. En Railway, crea un proyecto con **New Project → Deploy from GitHub repo** y elige el repositorio.
3. Añade la base de datos con **+ New → Database → PostgreSQL**.
4. En el servicio web, abre **Variables** y añade:

| Variable | Valor |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `SECRET_KEY` | una cadena larga y aleatoria (`python -c "import secrets;print(secrets.token_hex(32))"`) |
| `ANTHROPIC_API_KEY` | tu clave de la [Claude Console](https://console.anthropic.com) |
| `SIGNUP_CODE` | el código que darás a tus amigos |
| `ADMIN_EMAIL` | tu email (opcional: también te hace admin) |
| `DAILY_PHOTO_LIMIT` | `15` (fotos al día por amigo) |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` (o `claude-haiku-5-5` si quieres que sea más barato) |
| `APP_TZ` | `Asia/Tokyo` |

5. En **Settings → Networking → Generate Domain**, Railway te da la URL pública.
6. Abre la URL y crea tu cuenta (serás el administrador).

### Dominio propio (opcional)

En Railway, ve a **Settings → Networking → Custom Domain** y escribe, por ejemplo, `bocado.yenexbw.com`. Después, en Dynadot, añade el registro CNAME que te indique Railway.

## Controlar el gasto de la API

Cada foto es una llamada a la API de Claude. En la Claude Console (**Settings → Limits**) puedes poner un límite de gasto mensual para que nunca se pase de lo que decidas.

## Ejecutar en local

```bash
pip install -r requirements.txt
DEV=1 ANTHROPIC_API_KEY=sk-ant-... python app.py   # usa SQLite (bocado.db) si no hay DATABASE_URL
```

Se abre en http://localhost:5000

## API

| Método | Ruta | Uso |
|---|---|---|
| POST | `/api/register`, `/api/login`, `/api/logout` | Cuenta y sesión |
| GET / PUT / DELETE | `/api/me` | Datos, idioma, perfil y eliminar la cuenta |
| PUT | `/api/me/password` | Cambiar la contraseña |
| DELETE | `/api/me/data` | Borrar el perfil y el historial |
| GET | `/api/days?start=&end=` | Comidas por día |
| POST / DELETE | `/api/meals`, `/api/meals/<id>`, `/api/days/<día>` | Comidas |
| POST | `/api/analyze` | Foto o descripción → alimentos con gramos y kcal/100 g |
