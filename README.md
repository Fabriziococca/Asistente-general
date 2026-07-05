# Asistente de Operaciones y Auditoría Financiera Inteligente

Este proyecto consiste en un agente autónomo y asistente de operaciones desarrollado en Python, diseñado para integrarse con plataformas de mensajería en tiempo real (como Discord) y una base de datos relacional hospedada en la nube (PostgreSQL mediante NeonDB / asyncpg). Su propósito es automatizar flujos de atención al usuario, gestionar la entrega de accesos basados en privilegios, y realizar auditorías financieras autónomas en tiempo real a través de modelos de visión por inteligencia artificial (Generative AI).

El sistema está diseñado bajo principios de resiliencia, alta disponibilidad y mitigación de fallos, lo que lo convierte en una solución automatizada idónea para la autogestión de transacciones y soporte.

---

## 🚀 Características y Funcionalidades Clave

### 1. Sistema de Fidelización y Registro de Actividad
Integración con herramientas externas para el seguimiento de la interacción de los usuarios:
* **Conteo Automatizado**: Registra la actividad y el soporte orgánico de los usuarios dentro de la base de datos, previniendo registros duplicados o inserciones inválidas mediante transacciones atómicas.
* **Métricas de Participación**: Comandos para desplegar listados de clasificación (Top 10) con interfaces dinámicas (rich embeds) y consultas individuales de puntos acumulados.

### 2. Soporte Conversacional y Auditoría Financiera con IA
Gestión inteligente del ciclo de vida de los canales de soporte (tickets de atención) mediante agentes conversacionales y validación autónoma de archivos:
* **Arquitectura de Conmutación de IA (Failover Bunker)**: Para mitigar cuotas de uso y fallos de API, el bot implementa un pool de conmutación secuencial que rota a través de 5 modelos de lenguaje de última generación (`gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-flash-latest`) y alterna de forma dinámica entre múltiples claves de API configuradas en caso de interrupción del servicio.
* **Auditoría Financiera por Visión Artificial**:
  * **Procesamiento de Documentos**: Analiza de manera inmediata comprobantes en formatos de imagen o PDF cargados por los usuarios.
  * **Prevención de Fraude**: Compara datos sensibles como el destinatario de la transferencia, la entidad financiera de destino y las marcas de tiempo contra los parámetros de seguridad establecidos en la configuración del sistema.
  * **Mitigación de Capturas Duplicadas**: Emplea detección de firmas temporales e identificadores únicos de transacciones en el historial del canal para evitar el reenvío de comprobantes de pago previamente procesados.
  * **Cálculo Multidivisa**: Convierte y valida transacciones de diferentes monedas a valores de referencia (por ejemplo, pesos locales a equivalentes en dólares netos) según las reglas de negocio configuradas.
* **Asignación Automatizada de Permisos y Accesos**: Tras una validación exitosa de la transacción, el bot asigna de forma inmediata los roles y permisos del nivel adquirido en la plataforma y registra la transacción en la tabla de auditoría (`pagos`) de la base de datos PostgreSQL.
* **Circuit Breaker y Mitigación de Errores**: En caso de acumularse fallos consecutivos de validación automática (umbral configurado en 5 intentos), el sistema suspende temporalmente el agente de IA para ese canal, notifica directamente al administrador del sistema mediante pings y delega el control a un operador humano. Cuenta con comandos dedicados (como `/manual`) para forzar este comportamiento preventivamente.
* **Sistema de Billetera Virtual y Redención**: Permite a los usuarios utilizar puntos de interacción registrados para el canje manual de solicitudes de servicio, integrando flujos híbridos entre validación por IA y aprobación administrativa.

### 3. Tareas Automatizadas y Optimización de Recursos (Background Loops)
* **Gestión de Ciclo de Vida de Canales (Cleanup Task)**: Un proceso en segundo plano que se ejecuta periódicamente cada hora para limpiar recursos huérfanos:
  * Elimina canales de atención completados tras 24 horas de inactividad.
  * Cierra canales creados que no registren interacción de los usuarios tras las primeras 3 horas.
  * Remueve canales inactivos sin transacciones finalizadas tras 24 horas.
  * **Protección de Datos Críticos**: El algoritmo protege y conserva canales categorizados como solicitudes especiales o sugerencias en curso, evitando la pérdida accidental de requerimientos del cliente.
* **Comunicaciones Periódicas de Retención**: Módulos automatizados para refrescar y limpiar el historial de notificaciones en canales de difusión general, enviando avisos de actualización programados.

### 4. Estabilidad y Resiliencia en Producción
* **Servidor de Estado (Health Check / Keep Alive)**: Servidor web HTTP ligero que responde a solicitudes de estado para evitar la suspensión o el apagado de la instancia en plataformas PaaS (como Render).
* **Bypass de Restricciones Perimetrales (HTTP 429 / 1015)**: Implementa una máquina de estados con reintentos y retroceso exponencial progresivo (exponential backoff) para manejar de forma robusta los bloqueos temporales por límites de peticiones impuestos por proxies y firewalls (como Cloudflare) en entornos de nube.

---

## 📂 Estructura del Repositorio

* **[main.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/main.py)**: Orquestador y punto de entrada. Configura la base de datos relacional, carga de forma dinámica los submódulos, inicializa comandos de barra diagonal y levanta los servicios de resiliencia de conexión.
* **[keep_alive.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/keep_alive.py)**: Hilo secundario para mantener el servicio activo mediante respuestas de salud en red.
* **[requirements.txt](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/requirements.txt)**: Dependencias e integraciones externas.
* **[cogs/](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs)**: Módulos de lógica encapsulada.
  * **[cogs/bumps.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs/bumps.py)**: Módulo encargado de la lógica de gamificación, puntos de fidelidad y rankings.
  * **[cogs/tickets.py](file:///c:/Users/fabro/Documents/DS%20chaquetas/Contador-de-bumps/cogs/tickets.py)**: Core de operaciones, automatización de soporte por chat, integración con LLM multimodales y auditoría de documentos de transferencia.

---

## ⚙️ Configuración y Variables de Entorno

Debes crear un archivo `.env` en la raíz del proyecto para definir las credenciales y configuraciones del entorno de ejecución:

```env
DISCORD_TOKEN=TuTokenDeAccesoPlataforma
DATABASE_URL=postgresql://usuario:contraseña@servidor.neon.tech/nombre_db?sslmode=require

# Claves de API de IA para balanceo y rotación redundante
GEMINI_API_KEY=ClavePrincipalIA
GEMINI_API_KEY_2=ClaveSecundariaIA
GEMINI_API_KEY_3=ClaveTerciariaIA

# Puerto HTTP para el servicio de Health Check (Default: 8080)
PORT=8080
```

---

## 🗄️ Esquema de Base de Datos (SQL)

La aplicación inicializa de manera automática las siguientes estructuras relacionales al arrancar. El diseño de almacenamiento está optimizado para consultas concurrentes:

```sql
-- Tabla para el seguimiento de puntos e interacciones
CREATE TABLE IF NOT EXISTS bumps (
    user_id TEXT,
    guild_id TEXT,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

-- Tabla para la gestión del estado de los canales de soporte
CREATE TABLE IF NOT EXISTS tickets (
    channel_id BIGINT PRIMARY KEY,
    user_id BIGINT,
    estado TEXT DEFAULT 'abierto',
    ultimo_mensaje TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    hablo BOOLEAN DEFAULT FALSE
);

-- Tabla para el registro y auditoría de transacciones aprobadas
CREATE TABLE IF NOT EXISTS pagos (
    pago_id SERIAL PRIMARY KEY,
    user_id BIGINT,
    ticket_id BIGINT,
    monto NUMERIC(10, 2),
    moneda TEXT
);

-- Tabla general de usuarios registrados
CREATE TABLE IF NOT EXISTS usuarios (
    user_id BIGINT PRIMARY KEY
);
```

---

## 🛠️ Instalación y Despliegue Local

Sigue estos pasos para configurar el entorno de ejecución:

1. **Clonar el repositorio**:
   ```bash
   git clone https://github.com/Fabriziococca/Contador-de-bumps.git
   cd Contador-de-bumps
   ```

2. **Inicializar entorno virtual**:
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

4. **Configurar variables**:
   Copia el esquema del apartado [Configuración](#%EF%B8%8F-configuraci%C3%B3n-y-variables-de-entorno) en un archivo `.env`.

5. **Iniciar el bot**:
   ```bash
   python main.py
   ```

---

## 🚀 Despliegue Cloud (PaaS)

Para desplegar la aplicación en servicios como Render, Heroku o similares:
1. Crea un **Web Service** y conéctalo al repositorio.
2. Configura los siguientes comandos de despliegue:
   * **Build Command**: `pip install -r requirements.txt`
   * **Start Command**: `python main.py`
3. Registra las variables de entorno detalladas en el apartado de configuración. El servicio de Health Check enlazará automáticamente el puerto dinámico asignado por el hosting.
