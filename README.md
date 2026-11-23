# Bot de membresías para Telegram

Registra miembros, envía recordatorios, crea llaves y expulsa al vencer.

## Variables

| Variable | Obligatoria |
|---|---|
| BOT_TOKEN | Sí |
| ADMIN_IDS | Sí |
| GROUP_ID | No |
| DATABASE_URL | No; usa SQLite si no está definida |

Opcionales: `DIAS_MEMBRESIA=30`, `REMINDER_DAYS=5,2,1`, `REVISION_SEGUNDOS=60`, `TZ_OFFSET=-5`, `AUTO_REGISTRO=1`.

## Comandos

- `/start`, `/miid`, `/tiempo`, `/canjear LLAVE`, `/enlace`
- `/ayuda`, `/canales`, `/addcanal CHAT_ID [alias]`, `/quitarcanal #alias`
- `/llave 30d [#alias]`, `/llaves`, `/revocar CODIGO`
- `/pago ID [Nombre] [#alias]`, `/agregar ID DIAS [Nombre] [#alias]`
- `/estado ID [#alias]`, `/lista [#alias]`, `/usuarios [#alias]`
- `/vencer ID [#alias]`, `/quitar ID [#alias]`

## Desplegar

Conecta el repositorio privado a Railway, agrega PostgreSQL y configura las variables. Usa un `BOT_TOKEN` diferente al de cualquier otra copia del bot.
