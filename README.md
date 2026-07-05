# Asistente de Operaciones y Auditoría Financiera

Agente autónomo desarrollado en Python para integrarse con plataformas de mensajería en tiempo real y bases de datos PostgreSQL (NeonDB). Automatiza la gestión de canales de soporte, la fidelización de usuarios y la validación financiera de transacciones mediante Inteligencia Artificial (LLM).

---

## 🚀 Características Clave

* **Auditoría Financiera por Visión (IA)**: Validación en tiempo real de comprobantes de transferencia (imágenes/PDF). Compara destinatarios, entidades bancarias receptoras y marcas de tiempo para mitigar fraudes y capturas duplicadas.
* **Soporte y Entrega de Accesos**: Chatbot autónomo que atiende consultas frecuentes y asigna permisos de usuario automáticamente en la plataforma una vez verificada la transacción.
* **Búnker de IA (Failover & Circuit Breaker)**: Rotación automática entre un pool de 5 modelos de lenguaje y múltiples claves de API para evitar cuotas de Rate Limit. Ante fallos consecutivos (5 intentos), se desactiva la IA y se delega el caso a soporte humano.
* **Ciclo de Vida Autónomo**: Limpieza periódica en segundo plano que elimina canales completados (a las 24 horas) o inactivos (a las 3 horas), protegiendo solicitudes especiales vigentes.
* **Resiliencia en Producción**: Servidor de salud (Health Check) para hostings PaaS (Render) y lógica de reintentos con retroceso exponencial ante bloqueos perimetrales de red (HTTP 429).

---

## 📂 Estructura del Proyecto

* **[main.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/main.py)**: Punto de entrada, conexión a la DB, carga de cogs y control de reintentos de red.
* **[keep_alive.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/keep_alive.py)**: Servidor web HTTP ligero de Health Check.
* **[cogs/bumps.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs/bumps.py)**: Gamificación, registro de interacción de usuarios y rankings locales.
* **[cogs/tickets.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs/tickets.py)**: Núcleo de soporte por chat, motor LLM y auditoría visual de comprobantes.

---

## ⚙️ Configuración Rápida

### Variables de Entorno (`.env`)
```env
DISCORD_TOKEN=TuTokenDeDiscord
DATABASE_URL=postgresql://usuario:contraseña@servidor.neon.tech/db?sslmode=require
GEMINI_API_KEY=ClavePrincipal
GEMINI_API_KEY_2=ClaveSecundaria
PORT=8080
```

### Base de Datos (NeonDB / SQL)
```sql
CREATE TABLE IF NOT EXISTS bumps (user_id TEXT, guild_id TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, guild_id));
CREATE TABLE IF NOT EXISTS tickets (channel_id BIGINT PRIMARY KEY, user_id BIGINT, estado TEXT DEFAULT 'abierto', ultimo_mensaje TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, hablo BOOLEAN DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS pagos (pago_id SERIAL PRIMARY KEY, user_id BIGINT, ticket_id BIGINT, monto NUMERIC(10, 2), moneda TEXT);
CREATE TABLE IF NOT EXISTS usuarios (user_id BIGINT PRIMARY KEY);
```

---

## 🛠️ Instalación y Ejecución

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Ejecutar la aplicación
python main.py
```
