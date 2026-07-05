# Tito Calderón - Asistente General de Discord

Este proyecto consiste en un bot multipropósito de Discord, desarrollado en Python utilizando la librería **discord.py** y una base de datos relacional hospedada en **NeonDB (PostgreSQL)** mediante **asyncpg**. El bot actúa como asistente general del servidor, automatizando de forma inteligente la administración de puntos de apoyo (bumps), el soporte al usuario, y la validación financiera autónoma de compras de rangos y solicitudes de catálogo mediante Inteligencia Artificial.

---

## 🚀 Características Principales

### 1. Sistema de Bumps (Fidelización Orgánica)
Integración con el bot de **Disboard** para registrar el apoyo de la comunidad:
* **Conteo Automatizado**: Escucha las interacciones de Disboard e incrementa de forma segura el contador del usuario para ese servidor en la base de datos (evitando colisiones o registros falsos).
* **Comando `/ranking`**: Muestra una tabla clasificatoria (Top 10) estilizada con embeds dorados que indica los usuarios que más han apoyado con bumps.
* **Comando `/mispuntos`**: Permite a cualquier miembro consultar sus puntos individuales de forma privada (ephemeral).

### 2. Búnker de IA y Soporte Inteligente (Tickets Cog)
El bot administra canales de tickets creados dinámicamente (`ticket-` y `sug-`) implementando flujos de atención y un auditor de cobros automatizado:
* **Búnker de Modelos Multi-API Key**: Para evitar bloqueos por cuota de uso gratuito (Rate Limits), el bot rota secuencialmente a través de un pool de 5 modelos de Gemini (`gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-flash-latest`) y conmuta de forma automática entre múltiples API Keys configuradas en el entorno si detecta un fallo de servicio.
* **Auditoría Financiera por Visión de IA**:
  * Cuando un usuario envía un comprobante (captura de imagen o PDF) en el ticket, la IA analiza visualmente los datos.
  * **Filtro Anti-Fraude Estricto**: Valida obligatoriamente que el destinatario sea el titular de la cuenta (`Fabrizio Giovanni Cocca Ducay` o `sesarjavier28@gmail.com` en PayPal).
  * **Verificación de Entidad Receptora**: Exige que las transferencias en pesos argentinos (ARS) provengan o tengan destino final en `Uala Bank S.A.U` (Ualá), previniendo falsificaciones basadas en plantillas editadas de Mercado Pago u otras plataformas.
  * **Detección de Clones**: Rastrea identificadores únicos, horas y minutos repetidos en el historial para mitigar el reenvío de comprobantes viejos o duplicados.
  * **Validación de Divisas y Combos**: Convierte divisas internacionales a USD netos en PayPal para verificar si cubre el costo de los rangos individuales o combos definidos.
* **Entrega Automatizada de Roles**: Si el comprobante es válido, otorga inmediatamente los roles de Discord correspondientes (Diamante, Oro, Plata, o combinaciones) y registra la transacción en la tabla `pagos` de PostgreSQL.
* **Disyuntor de Fallos y Modo Manual**: Si el sistema automático detecta 5 fallos seguidos al auditar una imagen, desactiva la IA para ese ticket, alerta al administrador (`@titocalderon`) mencionándolo e informa al usuario que su caso pasa a revisión manual. También los administradores pueden activar el modo manual con el comando `/manual`.
* **Canje de Recompensas**: Permite a los usuarios canjear **30 puntos de bumps** por solicitudes únicas de catálogo. Al escribir *"Quiero canjear mis puntos"*, la IA conmutará a modo preventivo y el administrador podrá descontar los puntos mediante el comando administrativo `/canjear <usuario>`.

### 3. Tareas Automatizadas en Segundo Plano (Loops)
* **Limpieza y Ahorro de Base de Datos (`cleanup_tickets`)**: Ejecutado cada 1 hora.
  * Elimina los canales de tickets de rangos completados de forma automática tras 24 horas de inactividad.
  * Elimina canales abandonados que lleven 3 horas sin ningún mensaje inicial tras su creación.
  * Cierra tickets inactivos a las 24 horas si el usuario habló pero no completó una compra.
  * **Escudo de Sugerencias**: Protege permanentemente las peticiones de catálogo pagadas/iniciadas para evitar la pérdida de información y solicitudes del cliente.
* **Remarketing Semanal (`auto_promo_refresh`)**: Cada 7 días limpia el canal promocional de pings previos y envía un recordatorio automático pingueando a `@everyone` para notificar actualizaciones de contenido, posicionándose estratégicamente para no enterrar los botones del creador de tickets.

### 4. Estabilidad y Resiliencia en Producción
* **Keep Alive**: Servidor web HTTP local integrado mediante un hilo secundario (`BaseHTTPRequestHandler`) en el puerto de red configurable por entorno, ideal para mantener viva la aplicación ante health checks de plataformas PaaS como Render.
* **Escudo de Reintentos Anti-Cloudflare**: Ante errores típicos de red o bloqueos de Cloudflare (error HTTP 429 / 1015) al intentar iniciar sesión en Discord, el bot implementa un bucle de hasta 5 intentos con **espera y backoff exponencial progresivo** (30s, 60s, 120s...) para evitar caídas permanentes en el servidor de hosting.

---

## 📂 Estructura del Repositorio

* **[main.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/main.py)**: Archivo de entrada principal. Se encarga de inicializar el pool de conexiones NeonDB, asegurar la creación de tablas iniciales, cargar dinámicamente las extensiones (Cogs), sincronizar comandos Slash globales y manejar los reintentos de arranque y keep-alive.
* **[keep_alive.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/keep_alive.py)**: Servidor web en un hilo secundario que responde saludablemente a las solicitudes de ping HTTP de plataformas de despliegue para evitar que el bot se suspenda por inactividad.
* **[requirements.txt](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/requirements.txt)**: Lista de dependencias del proyecto (`discord.py`, `python-dotenv`, `asyncpg`, `google-generativeai`, `flask`).
* **[cogs/](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs)**: Directorio con módulos específicos del bot.
  * **[cogs/bumps.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs/bumps.py)**: Lógica y comandos relacionados al conteo de bumps de Disboard y visualización de rankings locales.
  * **[cogs/tickets.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs/tickets.py)**: Módulo de ventas, sistema inteligente de tickets, control de IA conversacional y auditor financiero basado en visión (Gemini).

---

## ⚙️ Configuración y Variables de Entorno

Para ejecutar este proyecto de forma local o en la nube, debes crear un archivo `.env` en la raíz del proyecto con la siguiente estructura:

```env
DISCORD_TOKEN=TuTokenDeDiscordAqui
DATABASE_URL=postgresql://usuario:contraseña@servidor.neon.tech/nombre_db?sslmode=require

# API Keys de Gemini para el balanceo y rotación (Puedes agregar varias iniciando con GEMINI_API_KEY)
GEMINI_API_KEY=TuClavePrincipalDeGemini
GEMINI_API_KEY_2=TuClaveSecundariaDeGemini
GEMINI_API_KEY_3=OtraClaveOpcional

# Puerto HTTP opcional para Render/keep_alive (Por defecto: 8080)
PORT=8080
```

---

## 🗄️ Base de Datos (Esquema SQL)

El bot está diseñado para inicializar de manera automática sus tablas esenciales si no existen. No obstante, a continuación se detallan las estructuras relacionales empleadas en NeonDB:

```sql
-- Tabla para el conteo de bumps
CREATE TABLE IF NOT EXISTS bumps (
    user_id TEXT,
    guild_id TEXT,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

-- Tabla para el estado de los tickets
CREATE TABLE IF NOT EXISTS tickets (
    channel_id BIGINT PRIMARY KEY,
    user_id BIGINT,
    estado TEXT DEFAULT 'abierto',
    ultimo_mensaje TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    hablo BOOLEAN DEFAULT FALSE
);

-- Tabla para el historial de transacciones validadas
CREATE TABLE IF NOT EXISTS pagos (
    pago_id SERIAL PRIMARY KEY,
    user_id BIGINT,
    ticket_id BIGINT,
    monto NUMERIC(10, 2),
    moneda TEXT
);

-- Tabla de usuarios registrados
CREATE TABLE IF NOT EXISTS usuarios (
    user_id BIGINT PRIMARY KEY
);
```

---

## 🛠️ Instalación y Despliegue Local

Sigue estos pasos para levantar el entorno de desarrollo localmente:

1. **Clonar el repositorio**:
   ```bash
   git clone https://github.com/Fabriziococca/Contador-de-bumps.git
   cd Contador-de-bumps
   ```

2. **Crear e inicializar un entorno virtual de Python**:
   * En Windows:
     ```bash
     python -m venv venv
     .\venv\Scripts\activate
     ```
   * En macOS/Linux:
     ```bash
     python3 -m venv venv
     source venv/bin/activate
     ```

3. **Instalar dependencias**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configurar el entorno**:
   Crea y completa el archivo `.env` según la sección de [Configuración](#%EF%B8%8F-configuraci%C3%B3n-y-variables-de-entorno).

5. **Iniciar el bot**:
   ```bash
   python main.py
   ```

---

## 🚀 Despliegue en Render (o similar)

1. En el dashboard de Render, crea un nuevo **Web Service**.
2. Vincula tu repositorio de GitHub `Contador-de-bumps`.
3. Configura los siguientes parámetros en Render:
   * **Runtime**: `Python`
   * **Build Command**: `pip install -r requirements.txt`
   * **Start Command**: `python main.py`
4. Añade tus variables de entorno en la sección **Environment**:
   * Configura `DISCORD_TOKEN`, `DATABASE_URL` y las correspondientes `GEMINI_API_KEY`s.
   * Render inyectará de forma automática la variable `PORT` (por lo general `10000`), la cual será capturada automáticamente por `keep_alive.py`.
