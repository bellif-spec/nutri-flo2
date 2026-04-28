# telegram_bot.py — Bot de Telegram (Lógica Completa)
import os
import re
import logging
import telebot
from datetime import datetime, timedelta

from database import get_db_connection, sumar_calorias_activas
from parsers import extract_calories_ocr

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID", "")


def start_telebot_thread():
    """Inicia el bot de Telegram en modo polling infinito."""
    if not TELEGRAM_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN no configurado. Bot desactivado.")
        return

    bot = telebot.TeleBot(TELEGRAM_TOKEN)
    logger.info("Bot de Telegram iniciado.")

    # ─── Helpers ─────────────────────────────────────────
    def get_auth_user(chat_id):
        conn = get_db_connection()
        user_row = conn.execute("SELECT id FROM Usuario WHERE telegram_id = ?", (str(chat_id),)).fetchone()
        conn.close()
        return user_row['id'] if user_row else None

    def auth_failed_msg(chat_id):
        return (f"❌ CUIDADO: La aplicación clínica no reconoce a tu usuario.\n\n"
                f"Tu número de Identidad Único es: {chat_id}\n"
                f"Pasale este número a tu Nutricionista o pegalo manualmente en "
                f"tu 'Ficha de Paciente' dentro del Dashboard para habilitar tu Bot.")

    def get_btn_volver():
        return telebot.types.InlineKeyboardButton("⬅️ Volver al Menú Principal", callback_data="btn_volver")

    def send_main_menu(message_or_chat_id, text="¡Hola! ¿Qué tarea querés realizar ahora?"):
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            telebot.types.InlineKeyboardButton("🔥 Gastadas", callback_data="menu_gastadas"),
            telebot.types.InlineKeyboardButton("🍴 Ingesta", callback_data="menu_ingesta")
        )
        markup.add(
            telebot.types.InlineKeyboardButton("📝 Nuevo Alimento", callback_data="menu_nuevo"),
            telebot.types.InlineKeyboardButton("📊 Balance Semanal", callback_data="menu_grafico")
        )
        chat_id = message_or_chat_id.chat.id if hasattr(message_or_chat_id, 'chat') else message_or_chat_id
        bot.send_message(chat_id, text, reply_markup=markup)

    def clean_txt(t):
        return t.lower().replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u')

    # ─── Handlers ────────────────────────────────────────
    @bot.message_handler(commands=['start', 'menu', 'volver'])
    def cmd_start(message):
        bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
        send_main_menu(message)

    @bot.callback_query_handler(func=lambda call: call.data == 'btn_volver')
    def btn_volver_handler(call):
        bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e:
            logger.debug(f"No se pudo limpiar markup: {e}")
        send_main_menu(call.message.chat.id, "Acción cancelada. Volviendo al menú principal:")

    @bot.callback_query_handler(func=lambda call: call.data in ['menu_gastadas', 'menu_ingesta', 'menu_nuevo', 'menu_grafico'])
    def main_menu_routing(call):
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e:
            logger.debug(f"No se pudo limpiar markup: {e}")

        if call.data == 'menu_grafico':
            generate_weekly_report(call.message.chat.id)
        elif call.data == 'menu_nuevo':
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.send_message(call.message.chat.id,
                "📝 _Asistente de Carga Manual_\n\n**Paso 1/6:** Escribí el nombre del alimento para el catálogo.",
                parse_mode="Markdown", reply_markup=markup)
            bot.register_next_step_handler(msg, wiz_nombre)
        else:
            markup = telebot.types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                telebot.types.InlineKeyboardButton("Hoy", callback_data=f"date_hoy_{call.data}"),
                telebot.types.InlineKeyboardButton("Ayer", callback_data=f"date_ayer_{call.data}")
            )
            markup.add(telebot.types.InlineKeyboardButton("Elegir otra fecha", callback_data=f"date_otra_{call.data}"))
            markup.add(get_btn_volver())
            bot.send_message(call.message.chat.id, "📅 ¿Para qué fecha es el registro?", reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('date_'))
    def process_date_selection(call):
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e:
            logger.debug(f"No se pudo limpiar markup: {e}")

        partes = call.data.split('_')
        tipo_fecha = partes[1]
        accion_origen = "_".join(partes[2:])

        if tipo_fecha == "hoy":
            fecha_str = datetime.now().strftime("%Y-%m-%d")
            route_action(call.message.chat.id, accion_origen, fecha_str)
        elif tipo_fecha == "ayer":
            fecha_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            route_action(call.message.chat.id, accion_origen, fecha_str)
        elif tipo_fecha == "otra":
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.send_message(call.message.chat.id, "Ingresa la fecha en formato DD/MM/YYYY:", reply_markup=markup)
            bot.register_next_step_handler(msg, parse_custom_date, accion_origen)

    def parse_custom_date(message, accion_origen):
        try:
            f = datetime.strptime(message.text.strip(), "%d/%m/%Y")
            route_action(message.chat.id, accion_origen, f.strftime("%Y-%m-%d"))
        except ValueError:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.reply_to(message, "❌ Formato inválido. Intenta de nuevo (DD/MM/YYYY):", reply_markup=markup)
            bot.register_next_step_handler(msg, parse_custom_date, accion_origen)

    def route_action(chat_id, accion_origen, fecha_str):
        id_usuario = get_auth_user(chat_id)
        if not id_usuario:
            return bot.send_message(chat_id, auth_failed_msg(chat_id))

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())

        if accion_origen == "menu_gastadas":
            msg = bot.send_message(chat_id,
                f"🔥 *Calorías Gastadas ({fecha_str})*\n\nEnviame un número de kcal quemadas o una **Foto** (captura del reloj/fitness app).",
                parse_mode="Markdown", reply_markup=markup)
            bot.register_next_step_handler(msg, process_gasto_input, id_usuario, fecha_str)
        elif accion_origen == "menu_ingesta":
            msg = bot.send_message(chat_id,
                f"🍴 *Registrar Ingesta ({fecha_str})*\n\nEscribí el **nombre del alimento** que consumiste:",
                parse_mode="Markdown", reply_markup=markup)
            bot.register_next_step_handler(msg, verify_food_db, id_usuario, fecha_str)

    # ─── Gasto / OCR ────────────────────────────────────
    def process_gasto_input(message, id_usuario, fecha_str):
        if message.content_type == 'photo':
            bot.reply_to(message, "📸 Imagen recibida. Analizando con OCR...")
            try:
                file_info = bot.get_file(message.photo[-1].file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                os.makedirs("tg_images", exist_ok=True)
                file_path = f"tg_images/photo_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
                with open(file_path, 'wb') as new_file:
                    new_file.write(downloaded_file)

                watch_stat = extract_calories_ocr(file_path)
                conn = get_db_connection()

                if isinstance(watch_stat, float):
                    sumar_calorias_activas(conn, id_usuario, fecha_str, watch_stat)
                    conn.commit()
                    bot.reply_to(message, f"🏃‍♂️🔥 ¡Gasto guardado! Detecté {watch_stat} kcal para el {fecha_str}.")
                elif isinstance(watch_stat, dict) and watch_stat:
                    total_kcal = 0
                    for d_str, cals in watch_stat.items():
                        sumar_calorias_activas(conn, id_usuario, d_str, cals)
                        total_kcal += cals
                    conn.commit()
                    bot.reply_to(message, f"🏃‍♂️🔥 ¡Multi-día! Inyecté {total_kcal} kcal en {len(watch_stat)} días.")
                else:
                    conn.execute("INSERT INTO Pendientes (id_usuario, tipo, contenido, fecha) VALUES (?, ?, ?, ?)",
                                (id_usuario, "imagen", file_path, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit()
                    bot.reply_to(message, "⚠️ No pude leer los números. Imagen enviada a la Cola Pendiente.")

                conn.close()
            except Exception as e:
                logger.error(f"Error procesando foto de gasto: {e}")
                bot.reply_to(message, f"Fallo al procesar imagen: {e}")
        else:
            texto = message.text.lower()
            num_match = re.search(r'(\d+[\.,]?\d*)', texto)
            if not num_match:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.add(get_btn_volver())
                msg = bot.reply_to(message, "❌ Número inválido. Enviame las calorías o foto.", reply_markup=markup)
                bot.register_next_step_handler(msg, process_gasto_input, id_usuario, fecha_str)
                return

            val = float(num_match.group(1).replace(',', '.'))
            conn = get_db_connection()
            sumar_calorias_activas(conn, id_usuario, fecha_str, val)
            conn.commit()
            conn.close()
            bot.reply_to(message, f"🔥 ¡Anotado! Sumé {val} kcal de actividad al {fecha_str}.")

    # ─── Ingesta / Búsqueda de Alimento ──────────────────
    def verify_food_db(message, id_usuario, fecha_str):
        nombre_buscado = message.text.strip()
        conn = get_db_connection()

        q_words = clean_txt(nombre_buscado).split()
        rows = conn.execute(
            "SELECT id, nombre, cal_100g, prot_100g, carb_100g, grasas_100g, "
            "cal_porcion, prot_porcion, carb_porcion, grasas_porcion, porcion_base_g "
            "FROM Biblioteca_Alimentos"
        ).fetchall()

        alimento = None
        for r in rows:
            if all(w in clean_txt(r['nombre']) for w in q_words):
                alimento = r
                break

        conn.close()

        if alimento:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.reply_to(message,
                f"✅ Encontré: **{alimento['nombre']}**.\n\n¿Qué cantidad? Escribe un número seguido de 'g' para gramos, o un número simple para porciones (ej: 150g o 1.5).",
                parse_mode="Markdown", reply_markup=markup)
            bot.register_next_step_handler(msg, process_food_qty, id_usuario, fecha_str, alimento)
        else:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(
                telebot.types.InlineKeyboardButton("📝 Cargar ahora", callback_data="menu_nuevo"),
                telebot.types.InlineKeyboardButton("⏳ Dejar pendiente", callback_data=f"pend_{nombre_buscado[:20]}")
            )
            markup.add(get_btn_volver())
            bot.reply_to(message, f"❌ El alimento '{nombre_buscado}' no existe en tu base local.", reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('pend_'))
    def pend_food_handler(call):
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e:
            logger.debug(f"No se pudo limpiar markup: {e}")
        nombre = call.data.split('pend_')[1]
        id_usuario = get_auth_user(call.message.chat.id)
        if id_usuario:
            conn = get_db_connection()
            conn.execute("INSERT INTO Pendientes (id_usuario, tipo, contenido, fecha) VALUES (?, ?, ?, ?)",
                        (id_usuario, "texto", f"Buscar/Cargar: {nombre}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
            bot.send_message(call.message.chat.id, f"📥 '{nombre}' guardado en Pendientes.")

    def process_food_qty(message, id_usuario, fecha_str, alimento):
        texto = message.text.lower()
        num_match = re.search(r'(\d+[\.,]?\d*)', texto)
        if not num_match:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.reply_to(message, "❌ Cantidad inválida. Intenta (ej: 150g o 1):", reply_markup=markup)
            bot.register_next_step_handler(msg, process_food_qty, id_usuario, fecha_str, alimento)
            return

        val = float(num_match.group(1).replace(',', '.'))
        is_gramos = bool(re.search(r'\d+\s*(g|gr|gramos|gramo)\b', texto))

        # Heurística: Si no escribió 'g' pero val >= 10, asumir gramos
        if not is_gramos and val >= 10:
            is_gramos = True

        c_100 = alimento['cal_100g']
        p_100 = alimento['prot_100g']
        cb_100 = alimento['carb_100g']
        g_100 = alimento['grasas_100g']

        if is_gramos:
            f = val / 100.0
            k, p, c, g = c_100 * f, p_100 * f, cb_100 * f, g_100 * f
            v_str = f"{val}g"
        else:
            k = (alimento['cal_porcion'] or 0) * val
            p = (alimento['prot_porcion'] or 0) * val
            c = (alimento['carb_porcion'] or 0) * val
            g = (alimento['grasas_porcion'] or 0) * val
            v_str = f"{val} porción/es"

        conn = get_db_connection()
        conn.execute(
            "INSERT INTO ConsumoDiario (id_usuario, fecha, id_alimento, tipo_ingreso, cantidad, calorias, proteinas, carbos, grasas) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id_usuario, fecha_str, alimento['id'], 'Telegram', val, k, p, c, g)
        )
        conn.commit()
        conn.close()

        bot.reply_to(message, f"✅ Ingesta cargada el {fecha_str}.\nComida: {alimento['nombre']} ({v_str})\nMacros: {k:.1f}kcal, {p:.1f}g P, {c:.1f}g HC, {g:.1f}g G.")
        logger.info(f"Ingesta registrada: {alimento['nombre']} ({v_str}) para usuario {id_usuario}")

    # ─── Wizard de Creación de Alimento ──────────────────
    def wiz_nombre(message):
        nombre = message.text.strip()
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message,
            f"Paso 2/6: ¿Para cuántos *Gramos (Porción)* vas a ingresarme los datos de la etiqueta de '{nombre}'?\n(Ej: Si dice 'Valores cada 30g', respondé '30').",
            parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_porcion, nombre)

    def wiz_porcion(message, nombre):
        try:
            porc = float(message.text.replace(',','.'))
        except ValueError:
            porc = 100.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, f"Anotado ({porc}g).\n\nPaso 3/6: ¿Cuántas *Calorías* tiene esa porción entera?",
            parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_kcal, nombre, porc)

    def wiz_kcal(message, nombre, porc):
        try:
            k = float(message.text.replace(',','.'))
        except ValueError:
            k = 0.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, "Paso 4/6: ¿Cuántos gramos de *Carbohidratos* leíste?",
            parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_carbos, nombre, porc, k)

    def wiz_carbos(message, nombre, porc, k):
        try:
            c = float(message.text.replace(',','.'))
        except ValueError:
            c = 0.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, "Paso 5/6: ¿Cuántos gramos de *Proteínas* leíste?",
            parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_prot, nombre, porc, k, c)

    def wiz_prot(message, nombre, porc, k, c):
        try:
            p = float(message.text.replace(',','.'))
        except ValueError:
            p = 0.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, "Paso 6/6: ¿Cuántos gramos de *Grasas Totales* leíste?",
            parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_grasas, nombre, porc, k, c, p)

    def wiz_grasas(message, nombre, porc, k, c, p):
        try:
            g = float(message.text.replace(',','.'))
        except ValueError:
            g = 0.0

        f = 100.0 / porc if porc > 0 else 1.0
        k100, p100, c100, g100 = k * f, p * f, c * f, g * f

        conn = get_db_connection()
        conn.execute(
            "INSERT INTO Biblioteca_Alimentos (nombre, porcion_base_g, cal_100g, prot_100g, carb_100g, grasas_100g, "
            "cal_porcion, prot_porcion, carb_porcion, grasas_porcion) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nombre, porc, k100, p100, c100, g100, k, p, c, g)
        )
        conn.commit()
        conn.close()

        bot.reply_to(message, f"🛒✅ ¡'{nombre}' grabado en la Biblioteca!")
        logger.info(f"Nuevo alimento creado via Telegram: {nombre}")

    # ─── Balance Semanal ────────────────────────────────
    def generate_weekly_report(chat_id):
        id_usuario = get_auth_user(chat_id)
        if not id_usuario:
            return bot.send_message(chat_id, auth_failed_msg(chat_id))

        conn = get_db_connection()
        end_date = datetime.now()
        start_date = end_date - timedelta(days=6)

        fechas = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        data = {f: {'in': 0, 'out': 0} for f in fechas}

        cur_in = conn.execute(
            "SELECT fecha, SUM(calorias) as total FROM ConsumoDiario WHERE id_usuario=? AND fecha >= ? GROUP BY fecha",
            (id_usuario, fechas[0])
        )
        for r in cur_in:
            if r['fecha'] in data:
                data[r['fecha']]['in'] = r['total'] or 0

        cur_out = conn.execute(
            "SELECT fecha, SUM(calorias_activas) as total FROM ActividadDiaria WHERE id_usuario=? AND fecha >= ? GROUP BY fecha",
            (id_usuario, fechas[0])
        )
        for r in cur_out:
            if r['fecha'] in data:
                data[r['fecha']]['out'] = r['total'] or 0

        conn.close()

        max_val = max((max(d['in'], d['out']) for d in data.values()), default=1) or 1

        msg_lines = ["📊 *Balance Semanal (Últimos 7 días)*\n"]
        dias_es = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']

        for f in reversed(fechas):
            d_obj = datetime.strptime(f, "%Y-%m-%d")
            dia_str = dias_es[d_obj.weekday()]
            v_in = data[f]['in']
            v_out = data[f]['out']

            blocks_in = int((v_in / max_val) * 8)
            blocks_out = int((v_out / max_val) * 8)

            bar_in = "🟥" * blocks_in + "⬜" * (8 - blocks_in)
            bar_out = "🟩" * blocks_out + "⬜" * (8 - blocks_out)

            msg_lines.append(f"📅 *{dia_str} {d_obj.strftime('%d/%m')}*")
            msg_lines.append(f"🍽️ {bar_in} {v_in:.0f} kcal")
            msg_lines.append(f"🔥 {bar_out} {v_out:.0f} kcal\n")

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        bot.send_message(chat_id, "\n".join(msg_lines), parse_mode="Markdown", reply_markup=markup)

    # ─── Catch-all Handlers ──────────────────────────────
    @bot.message_handler(content_types=['photo'])
    def photo_catch(message):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        bot.reply_to(message, "📸 Para escanear calorías por foto, usa primero el menú '🔥 Calorías Gastadas'.", reply_markup=markup)

    @bot.message_handler(func=lambda m: True)
    def catch_all(message):
        send_main_menu(message)

    # ─── Arranque ────────────────────────────────────────
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.critical(f"Bot de Telegram crasheó: {e}")
