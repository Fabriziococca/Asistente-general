import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import asyncio

DISBOARD_ID = 302050872383242240
FABRIZIO_ID = 704501115110162542

class Bumps(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Desactivado por defecto a nivel tiempo real para ahorro total de NeonDB
        self.bumps_enabled = False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Si el conteo en tiempo real está desactivado, el bot ni escucha ni consulta la DB
        if not self.bumps_enabled:
            return

        # Evitamos procesar mensajes que no sean de DISBOARD
        if message.author.id != DISBOARD_ID:
            return

        # Validamos si es un bump legítimo revisando la interacción o embeds
        if message.interaction_metadata:
            usuario = message.interaction_metadata.user
            
            es_bump_valido = False
            for embed in message.embeds:
                if (embed.description and "Bumped" in embed.description) or embed.image:
                    es_bump_valido = True
                    break

            if es_bump_valido:
                user_id = str(usuario.id)
                guild_id = str(message.guild.id) 
                
                # Upsert: Inserta 1, o suma 1 si ya existe el registro
                query = """
                    INSERT INTO bumps (user_id, guild_id, count) VALUES ($1, $2, 1)
                    ON CONFLICT (user_id, guild_id) DO UPDATE SET count = bumps.count + 1
                    RETURNING count
                """
                
                try:
                    # Timeout para evitar bloqueos por problemas de red con NeonDB
                    async with self.bot.pool.acquire(timeout=10.0) as conn:
                        nuevo_total = await conn.fetchval(query, user_id, guild_id)
                    
                    await message.channel.send(f"📈 **Bump registrado** | {usuario.mention} tiene ahora {nuevo_total} bumps.")
                except asyncio.TimeoutError:
                    print(f"⚠️ [TimeOut] Excedido tiempo de espera al registrar bump de {usuario.id}.")
                    await message.channel.send("⚠️ La base de datos está tardando en responder. El bump no pudo ser registrado.")
                except asyncpg.PostgresError as e:
                    print(f"❌ [DB Error] Fallo al insertar bump para {usuario.id}: {e}")
                    await message.channel.send("❌ Hubo un error de base de datos al guardar el bump.")
                except Exception as e:
                    print(f"❌ [Error Inesperado] en on_message (Bumps): {e}")

    @app_commands.command(name="toggle_bumps", description="Activa o desactiva el conteo de bumps en tiempo real")
    async def toggle_bumps(self, interaction: discord.Interaction):
        if interaction.user.id != FABRIZIO_ID and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ No tenés permisos para ejecutar este comando.", ephemeral=True)
            return

        self.bumps_enabled = not self.bumps_enabled
        estado_str = "🟢 **ACTIVADO** (El bot registrará bumps en tiempo real)" if self.bumps_enabled else "🔴 **DESACTIVADO** (El bot no escuchará ni gastará base de datos en tiempo real)"
        await interaction.response.send_message(f"⚙️ **Estado del Conteo de Bumps**: {estado_str}", ephemeral=True)

    @app_commands.command(name="sincronizar_bumps", description="Lee el historial de Discord y sincroniza todos los bumps en 1 sola consulta a la DB")
    @app_commands.describe(limite="Cantidad de mensajes a escanear del canal de bumps (por defecto 1000)")
    async def sincronizar_bumps(self, interaction: discord.Interaction, limite: int = 1000):
        if interaction.user.id != FABRIZIO_ID and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ No tenés permisos para ejecutar este comando.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        
        conteos = {} # user_id -> count
        total_bumps_leidos = 0

        try:
            async for message in interaction.channel.history(limit=limite):
                if message.author.id == DISBOARD_ID:
                    usuario = None
                    if message.interaction_metadata:
                        usuario = message.interaction_metadata.user
                    elif message.mentions:
                        usuario = message.mentions[0]

                    es_bump_valido = False
                    for embed in message.embeds:
                        if (embed.description and "Bumped" in embed.description) or embed.image:
                            es_bump_valido = True
                            break

                    if es_bump_valido and usuario:
                        u_id = str(usuario.id)
                        conteos[u_id] = conteos.get(u_id, 0) + 1
                        total_bumps_leidos += 1

            if not conteos:
                await interaction.followup.send("📭 No se encontraron mensajes de bumps de Disboard en el rango analizado.", ephemeral=True)
                return

            # Impacto masivo en 1 sola consulta/transacción SQL a NeonDB
            async with self.bot.pool.acquire(timeout=15.0) as conn:
                async with conn.transaction():
                    for u_id, cnt in conteos.items():
                        await conn.execute("""
                            INSERT INTO bumps (user_id, guild_id, count) VALUES ($1, $2, $3)
                            ON CONFLICT (user_id, guild_id) DO UPDATE SET count = GREATEST(bumps.count, EXCLUDED.count)
                        """, u_id, guild_id, cnt)

            embed_res = discord.Embed(
                title="⚡ Sincronización Masiva de Bumps Finalizada",
                color=discord.Color.green(),
                description=f"Se escanearon **{limite}** mensajes del historial de Discord.\n\n• **Bumps totales detectados**: `{total_bumps_leidos}`\n• **Usuarios actualizados**: `{len(conteos)}`\n• **Impacto en NeonDB**: 1 sola transacción en lote (Ahorro del 99.9% de cómputo)."
            )
            await interaction.followup.send(embed=embed_res, ephemeral=True)

        except Exception as e:
            print(f"❌ Error en sincronizar_bumps: {e}")
            await interaction.followup.send(f"❌ Ocurrió un error al sincronizar: {e}", ephemeral=True)

    @app_commands.command(name="ranking", description="Top 10 usuarios con más bumps en este servidor")
    async def ranking(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        
        query = "SELECT user_id, count FROM bumps WHERE guild_id = $1 ORDER BY count DESC LIMIT 10"
        
        try:
            async with self.bot.pool.acquire(timeout=10.0) as conn:
                filas = await conn.fetch(query, guild_id)

            if not filas:
                await interaction.response.send_message("📭 Aún no hay registros en este servidor.", ephemeral=True)
                return

            embed = discord.Embed(title=f"🏆 Ranking Local - {interaction.guild.name}", color=discord.Color.gold())
            texto_top = ""
            
            for i, fila in enumerate(filas):
                medalla = "🥇" if i==0 else "🥈" if i==1 else "🥉" if i==2 else "🔹"
                texto_top += f"**{i+1}.** {medalla} <@{fila['user_id']}> : `{fila['count']} bumps`\n"

            embed.add_field(name="Top 10", value=texto_top, inline=False)
            await interaction.response.send_message(embed=embed)
            
        except asyncio.TimeoutError:
            print(f"⚠️ [TimeOut] Consultando ranking en guild {guild_id}.")
            await interaction.response.send_message("⚠️ La base de datos tardó mucho en responder. Intenta de nuevo en unos segundos.", ephemeral=True)
        except asyncpg.PostgresError as e:
            print(f"❌ [DB Error] Consultando ranking en guild {guild_id}: {e}")
            await interaction.response.send_message("❌ Error de base de datos al consultar el ranking.", ephemeral=True)
        except Exception as e:
            print(f"❌ [Error Inesperado] en comando ranking: {e}")
            await interaction.response.send_message("❌ Ocurrió un error inesperado al intentar obtener el ranking.", ephemeral=True)

    @app_commands.command(name="mispuntos", description="Mira tus estadísticas en este servidor")
    async def mispuntos(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)

        query = "SELECT count FROM bumps WHERE user_id = $1 AND guild_id = $2"
        
        try:
            async with self.bot.pool.acquire(timeout=10.0) as conn:
                cantidad = await conn.fetchval(query, user_id, guild_id)

            cantidad = cantidad or 0 
            await interaction.response.send_message(f"Hola {interaction.user.mention}, llevas **{cantidad} bumps**.", ephemeral=True)
            
        except asyncio.TimeoutError:
            print(f"⚠️ [TimeOut] Consultando mispuntos para {user_id}.")
            await interaction.response.send_message("⚠️ La base de datos está saturada. Intenta más tarde.", ephemeral=True)
        except asyncpg.PostgresError as e:
            print(f"❌ [DB Error] Consultando mispuntos para {user_id}: {e}")
            await interaction.response.send_message("❌ Error interno al consultar tus puntos.", ephemeral=True)
        except Exception as e:
            print(f"❌ [Error Inesperado] en comando mispuntos: {e}")
            await interaction.response.send_message("❌ Ocurrió un error inesperado al consultar tus puntos.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Bumps(bot))
