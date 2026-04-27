import streamlit as st
import sqlite3
import pandas as pd
import requests
import pytesseract
from PIL import Image
import re
import threading
import os
from dotenv import load_dotenv
import telebot
from datetime import datetime, timedelta
import plotly.graph_objects as go
import plotly.express as px
import pdfplumber
import io

# Configuración y Variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID", "")
import os
import platform

if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
PG_URI = "postgresql://postgres.lvdapcakotthtnqwbscc:Flo1234Nutri@aws-1-us-west-2.pooler.supabase.com:6543/postgres"

# ======== 1. BASE DE DATOS (Multi-Tenant EAV) ========
import psycopg2
import psycopg2.extras
from sqlalchemy import create_engine
import pandas as pd

engine = create_engine(PG_URI.replace('postgres://', 'postgresql://'))

class DictRow(dict):
    def keys(self):
        return super().keys()
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

class FakeSqliteCursor:
    def __init__(self, cur):
        self.cur = cur
    
    def execute(self, query, params=None):
        query = query.replace('?', '%s')
        if params is not None:
            self.cur.execute(query, params)
        else:
            self.cur.execute(query)
        return self
        
    def fetchone(self):
        res = self.cur.fetchone()
        if res is not None: return DictRow(res)
        return None
        
    def fetchall(self):
        res = self.cur.fetchall()
        return [DictRow(r) for r in res]

class FakeSqliteConnection:
    def __init__(self):
        self.conn = psycopg2.connect(PG_URI)
        self.conn.autocommit = False
        
    def cursor(self):
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        return FakeSqliteCursor(cur)
        
    def execute(self, query, params=None):
        cur = self.cursor()
        return cur.execute(query, params)
        
    def commit(self):
        self.conn.commit()
        
    def close(self):
        self.conn.close()

original_read_sql_query = pd.read_sql_query
def custom_read_sql_query(sql, con, params=None, **kwargs):
    if isinstance(con, FakeSqliteConnection):
        sql = sql.replace('?', '%s')
        return original_read_sql_query(sql, engine, params=params, **kwargs)
    return original_read_sql_query(sql, con, params=params, **kwargs)

pd.read_sql_query = custom_read_sql_query

def get_db_connection():
    return FakeSqliteConnection()

# init_db ya no es necesario, las tablas ya están migradas en PostgreSQL.

# ======== 2. BOT DE TELEGRAM ========
def start_telebot_thread():
    if not TELEGRAM_TOKEN: return
    bot = telebot.TeleBot(TELEGRAM_TOKEN)
    
    def get_auth_user(chat_id):
        conn = get_db_connection()
        user_row = conn.execute("SELECT id FROM Usuario WHERE telegram_id = ?", (str(chat_id),)).fetchone()
        conn.close()
        return user_row['id'] if user_row else None

    def auth_failed_msg(chat_id):
        return f"❌ CUIDADO: La aplicación clínica no reconoce a tu usuario.\n\nTu número de Identidad Único es: {chat_id}\nPasale este número a tu Nutricionista o pegalo manualmente en tu 'Ficha de Paciente' dentro del Dashboard para habilitar tu Bot."

    def get_btn_volver():
        return telebot.types.InlineKeyboardButton("⬅️ Volver al Menú Principal", callback_data="btn_volver")

    def send_main_menu(message_or_chat_id, text="¡Hola! ¿Qué tarea querés realizar ahora?"):
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        btn_gastadas = telebot.types.InlineKeyboardButton("🔥 Gastadas", callback_data="menu_gastadas")
        btn_ingesta = telebot.types.InlineKeyboardButton("🍴 Ingesta", callback_data="menu_ingesta")
        btn_nuevo = telebot.types.InlineKeyboardButton("📝 Nuevo Alimento", callback_data="menu_nuevo")
        btn_grafico = telebot.types.InlineKeyboardButton("📊 Balance Semanal", callback_data="menu_grafico")
        markup.add(btn_gastadas, btn_ingesta)
        markup.add(btn_nuevo, btn_grafico)
        
        chat_id = message_or_chat_id.chat.id if hasattr(message_or_chat_id, 'chat') else message_or_chat_id
        bot.send_message(chat_id, text, reply_markup=markup)

    @bot.message_handler(commands=['start', 'menu', 'volver'])
    def cmd_start(message):
        bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
        send_main_menu(message)

    @bot.callback_query_handler(func=lambda call: call.data == 'btn_volver')
    def btn_volver_handler(call):
        bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
        send_main_menu(call.message.chat.id, "Acción cancelada. Volviendo al menú principal:")

    @bot.callback_query_handler(func=lambda call: call.data in ['menu_gastadas', 'menu_ingesta', 'menu_nuevo', 'menu_grafico'])
    def main_menu_routing(call):
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
        
        if call.data == 'menu_grafico':
            generate_weekly_report(call.message.chat.id)
        elif call.data == 'menu_nuevo':
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.send_message(call.message.chat.id, "📝 _Asistente de Carga Manual_\n\n**Paso 1/6:** Escribí el nombre del alimento para el catálogo.", parse_mode="Markdown", reply_markup=markup)
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
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
        
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
        except:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.reply_to(message, "❌ Formato inválido. Intenta de nuevo (DD/MM/YYYY):", reply_markup=markup)
            bot.register_next_step_handler(msg, parse_custom_date, accion_origen)

    def route_action(chat_id, accion_origen, fecha_str):
        id_usuario = get_auth_user(chat_id)
        if not id_usuario: return bot.send_message(chat_id, auth_failed_msg(chat_id))
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        
        if accion_origen == "menu_gastadas":
            msg = bot.send_message(chat_id, f"🔥 *Calorías Gastadas ({fecha_str})*\n\nEnviame un número de kcal quemadas o una **Foto** (captura del reloj/fitness app).", parse_mode="Markdown", reply_markup=markup)
            bot.register_next_step_handler(msg, process_gasto_input, id_usuario, fecha_str)
        elif accion_origen == "menu_ingesta":
            msg = bot.send_message(chat_id, f"🍴 *Registrar Ingesta ({fecha_str})*\n\nEscribí el **nombre del alimento** que consumiste:", parse_mode="Markdown", reply_markup=markup)
            bot.register_next_step_handler(msg, verify_food_db, id_usuario, fecha_str)

    def process_gasto_input(message, id_usuario, fecha_str):
        if message.content_type == 'photo':
            bot.reply_to(message, "📸 Imagen recibida. Analizando con OCR para sumarlo a tus calorías activas...")
            try:
                file_info = bot.get_file(message.photo[-1].file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                os.makedirs("tg_images", exist_ok=True)
                file_path = f"tg_images/photo_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
                with open(file_path, 'wb') as new_file: new_file.write(downloaded_file)
                
                watch_stat = extract_calories_ocr(file_path)
                conn = get_db_connection()
                if isinstance(watch_stat, float):
                    conn.execute("INSERT INTO ActividadDiaria (id_usuario, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING", (id_usuario, fecha_str))
                    conn.execute("UPDATE ActividadDiaria SET calorias_activas = calorias_activas + ? WHERE fecha = ? AND id_usuario=?", (watch_stat, fecha_str, id_usuario))
                    conn.commit()
                    bot.reply_to(message, f"🏃‍♂️🔥 ¡Gasto guardado automágicamente! Detecté {watch_stat} kcal para el {fecha_str}.")
                elif isinstance(watch_stat, dict) and watch_stat:
                    total_kcal = 0
                    for d_str, cals in watch_stat.items():
                        conn.execute("INSERT INTO ActividadDiaria (id_usuario, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING", (id_usuario, d_str))
                        conn.execute("UPDATE ActividadDiaria SET calorias_activas = calorias_activas + ? WHERE fecha = ? AND id_usuario=?", (cals, d_str, id_usuario))
                        total_kcal += cals
                    conn.commit()
                    bot.reply_to(message, f"🏃‍♂️🔥 ¡Modo Apple Fitness activado! Inyecté {total_kcal} kcal distribuidas en {len(watch_stat)} días.")
                else:
                    conn.execute("INSERT INTO Pendientes (id_usuario, tipo, contenido, fecha) VALUES (?, ?, ?, ?)", (id_usuario, "imagen", file_path, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit()
                    bot.reply_to(message, "⚠️ No pude leer los números claramente. La imagen fue enviada a la Cola Pendiente en Streamlit.")
                conn.close()
            except Exception as e:
                bot.reply_to(message, f"Fallo al procesar imagen: {e}")
        else:
            texto = message.text.lower()
            num_match = re.search(r'(\d+[\.,]?\d*)', texto)
            if not num_match:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.add(get_btn_volver())
                msg = bot.reply_to(message, "❌ No escribiste un número válido. Enviame las calorías o foto.", reply_markup=markup)
                bot.register_next_step_handler(msg, process_gasto_input, id_usuario, fecha_str)
                return
            
            val = float(num_match.group(1).replace(',', '.'))
            conn = get_db_connection()
            conn.execute("INSERT INTO ActividadDiaria (id_usuario, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING", (id_usuario, fecha_str))
            conn.execute("UPDATE ActividadDiaria SET calorias_activas = calorias_activas + ? WHERE fecha = ? AND id_usuario=?", (val, fecha_str, id_usuario))
            conn.commit(); conn.close()
            bot.reply_to(message, f"🔥 ¡Anotado! Sumé {val} kcal de actividad al {fecha_str}.")

    def verify_food_db(message, id_usuario, fecha_str):
        nombre_buscado = message.text.strip()
        conn = get_db_connection()
        
        def clean_txt(t):
            return t.lower().replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u')
            
        q_words = clean_txt(nombre_buscado).split()
        rows = conn.execute("SELECT id, nombre, cal_100g, prot_100g, carb_100g, grasas_100g, cal_porcion, prot_porcion, carb_porcion, grasas_porcion, porcion_base_g FROM Biblioteca_Alimentos").fetchall()
        
        alimento = None
        for r in rows:
            r_cl = clean_txt(r['nombre'])
            if all(w in r_cl for w in q_words):
                alimento = r
                break
        
        conn.close()
        
        if alimento:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.reply_to(message, f"✅ Encontré: **{alimento['nombre']}**.\n\n¿Qué cantidad ingeriste? Escribe un número seguido de 'g' para gramos, o un número simple para porciones (ej: 150g o 1.5).", parse_mode="Markdown", reply_markup=markup)
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
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
        nombre = call.data.split('pend_')[1]
        id_usuario = get_auth_user(call.message.chat.id)
        if id_usuario:
            conn = get_db_connection()
            conn.execute("INSERT INTO Pendientes (id_usuario, tipo, contenido, fecha) VALUES (?, ?, ?, ?)", (id_usuario, "texto", f"Buscar/Cargar: {nombre}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit(); conn.close()
            bot.send_message(call.message.chat.id, f"📥 '{nombre}' guardado en la tabla de Pendientes. Lo revisarás más tarde en la PC.")

    def process_food_qty(message, id_usuario, fecha_str, alimento):
        texto = message.text.lower()
        num_match = re.search(r'(\d+[\.,]?\d*)', texto)
        if not num_match: 
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(get_btn_volver())
            msg = bot.reply_to(message, "❌ Cantidad inválida. Intenta nuevamente (ej: 150g o 1):", reply_markup=markup)
            bot.register_next_step_handler(msg, process_food_qty, id_usuario, fecha_str, alimento)
            return
             
        val = float(num_match.group(1).replace(',', '.'))
        is_gramos = bool(re.search(r'\d+\s*(g|gr|gramos|gramo)\b', texto))
        
        # Heurística inteligente: Si no escribió 'g' pero el número es >= 10, lo más probable es que se refiera a gramos (ej: "100" -> 100g)
        if not is_gramos and val >= 10:
            is_gramos = True
        
        c_100, p_100, cb_100, g_100 = alimento['cal_100g'], alimento['prot_100g'], alimento['carb_100g'], alimento['grasas_100g']
        
        if is_gramos:
            f = val / 100.0
            k, p, c, g = c_100 * f, p_100 * f, cb_100 * f, g_100 * f
            cantidad_db = val
            v_str = f"{val}g"
        else:
            # Es porciones
            k = (alimento['cal_porcion'] or 0) * val
            p = (alimento['prot_porcion'] or 0) * val
            c = (alimento['carb_porcion'] or 0) * val
            g = (alimento['grasas_porcion'] or 0) * val
            cantidad_db = val
            v_str = f"{val} porción/es"
                
        conn = get_db_connection()
        conn.execute("INSERT INTO ConsumoDiario (id_usuario, fecha, id_alimento, tipo_ingreso, cantidad, calorias, proteinas, carbos, grasas) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                    (id_usuario, fecha_str, alimento['id'], 'Telegram', cantidad_db, k, p, c, g))
        conn.commit(); conn.close()
        
        bot.reply_to(message, f"✅ Ingesta cargada el {fecha_str}.\nComida: {alimento['nombre']} ({v_str})\nMacros: {k:.1f}kcal, {p:.1f}g P, {c:.1f}g HC, {g:.1f}g G.")

    def wiz_nombre(message):
        nombre = message.text.strip()
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, f"Paso 2/6: ¿Para cuántos *Gramos (Porción)* vas a ingresarme los datos de la etiqueta de '{nombre}'?\n(Ej: Si dice 'Valores cada 30g', respondé '30').", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_porcion, nombre)

    def wiz_porcion(message, nombre):
        try: porc = float(message.text.replace(',','.'))
        except: porc = 100.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, f"Anotado ({porc}g).\n\nPaso 3/6: ¿Cuántas *Calorías* tiene esa porción entera?", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_kcal, nombre, porc)

    def wiz_kcal(message, nombre, porc):
        try: k = float(message.text.replace(',','.'))
        except: k = 0.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, f"Paso 4/6: ¿Cuántos gramos de *Carbohidratos* leíste?", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_carbos, nombre, porc, k)

    def wiz_carbos(message, nombre, porc, k):
        try: c = float(message.text.replace(',','.'))
        except: c = 0.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, f"Paso 5/6: ¿Cuántos gramos de *Proteínas* leíste?", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_prot, nombre, porc, k, c)

    def wiz_prot(message, nombre, porc, k, c):
        try: p = float(message.text.replace(',','.'))
        except: p = 0.0
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, f"Paso 6/6: ¿Cuántos gramos de *Grasas Totales* leíste?", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, wiz_grasas, nombre, porc, k, c, p)

    def wiz_grasas(message, nombre, porc, k, c, p):
        try: g = float(message.text.replace(',','.'))
        except: g = 0.0
        
        f = 100.0 / porc if porc > 0 else 1.0
        k100, p100, c100, g100 = k*f, p*f, c*f, g*f
        
        conn = get_db_connection()
        conn.execute("INSERT INTO Biblioteca_Alimentos (nombre, porcion_base_g, cal_100g, prot_100g, carb_100g, grasas_100g, cal_porcion, prot_porcion, carb_porcion, grasas_porcion) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                    (nombre, porc, k100, p100, c100, g100, k, p, c, g))
        conn.commit(); conn.close()
        
        bot.reply_to(message, f"🛒✅ ¡Alimento grabado! '{nombre}' se guardó en la Biblioteca.")

    def generate_weekly_report(chat_id):
        id_usuario = get_auth_user(chat_id)
        if not id_usuario: return bot.send_message(chat_id, auth_failed_msg(chat_id))
        
        conn = get_db_connection()
        end_date = datetime.now()
        start_date = end_date - timedelta(days=6)
        
        fechas = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        data = {f: {'in': 0, 'out': 0} for f in fechas}
        
        cur_in = conn.execute("SELECT fecha, SUM(calorias) as total FROM ConsumoDiario WHERE id_usuario=? AND fecha >= ? GROUP BY fecha", (id_usuario, fechas[0]))
        for r in cur_in:
            if r['fecha'] in data: data[r['fecha']]['in'] = r['total'] or 0
            
        cur_out = conn.execute("SELECT fecha, SUM(calorias_activas) as total FROM ActividadDiaria WHERE id_usuario=? AND fecha >= ? GROUP BY fecha", (id_usuario, fechas[0]))
        for r in cur_out:
            if r['fecha'] in data: data[r['fecha']]['out'] = r['total'] or 0
            
        conn.close()
        
        max_val = 0
        for d in data.values():
            max_val = max(max_val, d['in'], d['out'])
            
        if max_val == 0: max_val = 1
        
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

    @bot.message_handler(content_types=['photo'])
    def photo_catch(message):
        # Handle loose photos
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(get_btn_volver())
        msg = bot.reply_to(message, "📸 Si quieres escanear calorías por foto, usa primero el menú '🔥 Calorías Gastadas'.", reply_markup=markup)

    @bot.message_handler(func=lambda m: True)
    def catch_all(message):
        send_main_menu(message)
        
    try: bot.infinity_polling()
    except: pass


if 'tg_thread' not in st.session_state and TELEGRAM_TOKEN and TELEGRAM_USER_ID:
    threading.Thread(target=start_telebot_thread, daemon=True).start()
    st.session_state['tg_thread'] = True

# ======== 3. PARSERS (API OFF y PDF Clínica) ========
def buscar_alimento_off_api(termino):
    url = f"https://world.openfoodfacts.org/cgi/search.pl?search_terms={termino}&search_simple=1&action=process&json=1"
    headers = {'User-Agent': 'MiNutriApp/5.2 (Windows)'}
    try:
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        return response.json().get('products', [])
    except requests.exceptions.HTTPError as he:
        if response.status_code >= 500:
            return {"error": "🌐 El servidor de OpenFoodFacts en Francia se encuentra colapsado momentáneamente (Error 50x).\n\n💡 Alternativa: Como importaste ARGENFOODS, andá a 'Declarar Consumo Diario' y buscá tu alimento localmente desplegando el selector (e.g. '[AF] Huevo de Gallina, Entero')."}
        return {"error": str(he)}
    except Exception as e:
        return {"error": f"Falla de Interfaz de Nube: {str(e)}"}

def extract_calories_ocr(image_path):
    try:
        import PIL.ImageOps
        if not os.path.exists(pytesseract.pytesseract.tesseract_cmd): return "Tesseract OCR no enrutado."
        os.environ['TESSDATA_PREFIX'] = os.path.join(os.getcwd(), 'tessdata')
        
        # PREPROCESAMIENTO: Invertir colores (Apple Fitness es oscuro, Tesseract necesita texto oscuro sobre blanco)
        img = Image.open(image_path).convert('L')
        img = PIL.ImageOps.invert(img)
        text = pytesseract.image_to_string(img, lang='spa+eng')
        
        # 1. Intento Multi-Día Apple Fitness
        matches = re.findall(r'(\d{1,3}(?:[.,]\d{3})*|\d+)\s*[C<]?A[L1]\s*([A-Za-z]+|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', text, re.IGNORECASE)
        if matches:
            resultados = {}
            hoy_dt = datetime.now()
            dias_semana = {
                'monday': 0, 'lunes': 0, 'tuesday': 1, 'martes': 1,
                'wednesday': 2, 'miercoles': 2, 'miércoles': 2,
                'thursday': 3, 'jueves': 3, 'friday': 4, 'viernes': 4,
                'saturday': 5, 'sabado': 5, 'sábado': 5, 'sunday': 6, 'domingo': 6
            }
            
            for cal_str, dia_str in matches:
                c_clean = cal_str.replace(',', '').replace('.', '')
                try: calorias = float(c_clean)
                except: continue
                
                dia_str_lower = dia_str.lower().strip()
                fecha_str = None
                
                if dia_str_lower in dias_semana:
                    dia_target = dias_semana[dia_str_lower]
                    diferencia = (hoy_dt.weekday() - dia_target) % 7
                    fecha_target = hoy_dt - timedelta(days=diferencia)
                    fecha_str = fecha_target.strftime("%Y-%m-%d")
                elif re.match(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', dia_str):
                    parts = re.split(r'[/-]', dia_str)
                    if len(parts) == 3:
                        p1, p2, p3 = int(parts[0]), int(parts[1]), int(parts[2])
                        y = p3 + 2000 if p3 < 100 else p3
                        try:
                            # Intento M/D/Y primero (Formato Apple Fitness EEUU)
                            fecha_target = datetime(y, p1, p2)
                            fecha_str = fecha_target.strftime("%Y-%m-%d")
                        except ValueError:
                            try:
                                # Intento D/M/Y (Formato Español)
                                fecha_target = datetime(y, p2, p1)
                                fecha_str = fecha_target.strftime("%Y-%m-%d")
                            except: pass
                
                if fecha_str:
                    if fecha_str in resultados: resultados[fecha_str] += calorias
                    else: resultados[fecha_str] = calorias
            if resultados: return resultados
            
        # 2. Fallback Extracción Diaria Clásica
        m = re.search(r'(?i)activas.*?(\d+[\.,]?\d*)', text) or re.search(r'(?i)moverse.*?(\d+[\.,]?\d*)', text)
        return float(m.group(1).replace(',', '.')) if m else "Error Biométrico."
    except Exception as e: return f"Error de lectura {e}"


# OCR Nutricional removido por Fallas de Tesseract en Empaques.

def parsear_laboratorio_pdf(archivo_bytes):
    marcadores_detectados = []
    try:
        with pdfplumber.open(io.BytesIO(archivo_bytes)) as pdf:
            text_completo = ""
            for page in pdf.pages:
                text = page.extract_text()
                if text: text_completo += text + "\n"
                
            estado_marcador = None
            for line in text_completo.split('\n'):
                line = line.strip()
                
                # Regex 1: Busca identificador numeral de CIBIC (Ej: "133 CALCEMIA")
                m_tit = re.match(r'^(\d{2,4})\s+([A-Z0-9\-\s\(\)]+)$', line)
                if m_tit and "COPIA DIGITAL" not in line:
                    estado_marcador = m_tit.group(2).strip()
                    continue
                
                # Regex 2: Busca "Valor Hallado:" pegado al marcador titular
                m_val = re.search(r'(?i)Valor\shallado[\:\.]*\s*([\d\.\,]+)\s*([a-zA-Z\/\%μµ]+)?', line)
                if m_val and estado_marcador:
                    v = m_val.group(1).replace(',', '.')
                    u = m_val.group(2) if m_val.group(2) else ""
                    try:
                        marcadores_detectados.append({"marcador": estado_marcador, "valor": float(v), "unidad": u.strip(), "ref_min": None, "ref_max": None})
                    except: pass
                    estado_marcador = None # Resetea para no pegar el mismo valor a otro
                    continue
                
                # Regex 3 (Heurística Fallback Clásica genérica)
                m_gen = re.search(r'^([a-zA-ZáéíóúÁÉÍÓÚñÑ\-\_]{4,35})\s+([\d\.\,]+)\s*([a-zA-Z\/\%μµ]{1,10})?', line)
                if m_gen and not estado_marcador:
                    n = m_gen.group(1).strip()
                    v2 = m_gen.group(2).replace(',', '.')
                    u2 = m_gen.group(3) if m_gen.group(3) else ""
                    if not any(f in n.lower() for f in ['fecha', 'paciente', 'edad', 'médico', 'página', 'sexo', 'impreso', 'referencia']):
                        try:
                            # Solo agregamos si no es basura y si no existe ya para evitar duplicados en el df final
                            if not any(x['marcador'] == n for x in marcadores_detectados):
                                marcadores_detectados.append({"marcador": n.title(), "valor": float(v2), "unidad": u2.strip(), "ref_min": None, "ref_max": None})
                        except: pass
                        
                # Regex 4: Busca los límites de Referencia reportados debajo o al lado
                m_ref_rango = re.search(r'(?i)Valor\s+de\s+Referencia[\:\.]*\s*([\d\.\,]+)\s*(?:a|\-)\s*([\d\.\,]+)', line)
                if m_ref_rango and marcadores_detectados:
                    try:
                        marcadores_detectados[-1]['ref_min'] = float(m_ref_rango.group(1).replace(',', '.'))
                        marcadores_detectados[-1]['ref_max'] = float(m_ref_rango.group(2).replace(',', '.'))
                    except: pass
                    continue
                
                m_ref_menor = re.search(r'(?i)Valor\s+de\s+Referencia[\:\.]*\s*(?:\<|menor|hasta)\s*([\d\.\,]+)', line)
                if m_ref_menor and marcadores_detectados:
                    try:
                        marcadores_detectados[-1]['ref_min'] = 0.0
                        marcadores_detectados[-1]['ref_max'] = float(m_ref_menor.group(1).replace(',', '.'))
                    except: pass
                    continue
                        
    except Exception as e:
        return {"error": str(e)}
    return pd.DataFrame(marcadores_detectados)

# ======== 4. UI STREAMLIT ========
st.set_page_config(page_title="Mi Nutri App Clínica", layout="wide", page_icon="🧬")

light_dashboard_css = """
<style>
/* Sidebar button text alignment */
[data-testid="stSidebar"] button p { text-align: left; font-size: 1.05rem; }
[data-testid="stSidebar"] button { justify-content: flex-start; border: none; }

/* Dashboard Cards */
.light-card { 
    background-color: #FFFFFF; 
    border-radius: 20px; 
    padding: 1.2rem; 
    margin-bottom: 1rem; 
    box-shadow: 0 4px 12px rgba(0,0,0,0.03); 
    border: 1px solid #EAEAEA;
    height: 100%;
}
.light-card-primary { 
    background-color: #175e4c; 
    border-radius: 20px; 
    padding: 1.2rem; 
    margin-bottom: 1rem; 
    box-shadow: 0 4px 15px rgba(23, 94, 76, 0.2); 
    height: 100%;
}
.light-card-primary p, .light-card-primary div, .light-card-primary span { color: #FFFFFF !important; }
.card-title { font-size: 1rem; font-weight: 600; color: #555555 !important; margin-bottom: 0.5rem; }
.card-title-primary { font-size: 1rem; font-weight: 600; color: #e0f2eb !important; margin-bottom: 0.5rem; }
.card-value { font-size: 2.2rem; font-weight: bold; color: #222222 !important; line-height: 1.2; }
.card-value-primary { font-size: 2.2rem; font-weight: bold; color: #FFFFFF !important; line-height: 1.2; }
.card-sub { font-size: 0.85rem; color: #888888 !important; font-weight: 500; }
.card-sub-primary { font-size: 0.85rem; color: #a3d9c5 !important; font-weight: 500; }
</style>
"""
st.markdown(light_dashboard_css, unsafe_allow_html=True)

def create_weekly_chart(df_hist):
    fig = go.Figure()
    
    text_cons = [f"<b>{val:,.0f}</b><br>Calorías Consumidas" if val > 0 else "" for val in df_hist['calorias_consumidas']]
    text_act = [f"<b>{val:,.0f}</b><br>Calorías Gastadas" if val > 0 else "" for val in df_hist['calorias_activas']]
    
    # Extraemos fechas cortas para mejor visualización
    fechas_short = pd.to_datetime(df_hist['fecha']).dt.strftime('%d/%m')
    
    fig.add_trace(go.Bar(
        x=fechas_short, y=df_hist['calorias_consumidas'], name='🍽️ CALORIAS CONSUMIDAS', 
        marker_color='#B4D330', width=0.38, marker_line_width=0,
        text=text_cons, textposition='outside', textfont=dict(size=12, color='#222')
    ))
    
    fig.add_trace(go.Bar(
        x=fechas_short, y=df_hist['calorias_activas'], name='🏃‍♀️ CALORIAS GASTADAS', 
        marker_color='#3C8E86', width=0.38, marker_line_width=0,
        text=text_act, textposition='outside', textfont=dict(size=12, color='#222')
    ))
    
    fig.update_layout(
        barmode='group', paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', 
        margin=dict(t=50, b=20, l=10, r=10), height=380, bargap=0.15,
        xaxis=dict(showgrid=False, zeroline=False, tickfont=dict(size=14, color='#555')), 
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="center", x=0.5, font=dict(size=13))
    )
    
    # Ajustar el tope del gráfico para que entren los textos outside
    max_y = max(df_hist['calorias_consumidas'].max(), df_hist['calorias_activas'].max()) * 1.2
    fig.update_yaxes(range=[0, max_y])
    return fig

@st.dialog("📊 Balance Semanal de Calorías (Últimos 7 días)")
def mostrar_grafico_semanal(u_id):
    conn = get_db_connection()
    query_hist_cons = "SELECT fecha, SUM(calorias) as calorias_consumidas, SUM(proteinas) as proteinas, SUM(carbos) as carbos, SUM(grasas) as grasas FROM ConsumoDiario WHERE id_usuario = ? GROUP BY fecha ORDER BY fecha DESC LIMIT 7"
    df_cons = pd.read_sql_query(query_hist_cons, conn, params=(u_id,))
    query_hist_act = "SELECT fecha, calorias_activas FROM ActividadDiaria WHERE id_usuario = ? ORDER BY fecha DESC LIMIT 7"
    df_act = pd.read_sql_query(query_hist_act, conn, params=(u_id,))
    conn.close()
    
    if df_cons.empty:
        st.info("No hay datos históricos para graficar.")
    else:
        df_hist = pd.merge(df_cons, df_act, on="fecha", how="left").fillna(0).sort_values("fecha")
        st.plotly_chart(create_weekly_chart(df_hist), use_container_width=True, config={'displayModeBar': False})

def main():
    conn = get_db_connection()
    pacientes = pd.read_sql_query("SELECT id, nombre FROM Usuario", conn)
    
    st.sidebar.title("🧬 Suite Clínica")
    
    # ------------------
    # MANEJO MULTI PACIENTE
    # ------------------
    if pacientes.empty:
        st.sidebar.warning("No hay pacientes.")
        st.session_state['active_user'] = None
    else:
        opciones_p = [f"{row['nombre']} (ID: {row['id']})" for _, row in pacientes.iterrows()]
        
        # Recuperar estado previo o elegir primero
        old_idx = 0
        if 'active_user' in st.session_state and st.session_state['active_user']:
            try:
                # buscar indice en array
                target_str = f"{st.session_state['active_user_name']} (ID: {st.session_state['active_user']})"
                if target_str in opciones_p: old_idx = opciones_p.index(target_str)
            except: pass
            
        paciente_seleccionado = st.sidebar.selectbox("🔎 Filtrar Datos Clínicos de:", opciones_p, index=old_idx)
        
        id_pac = int(paciente_seleccionado.split("ID: ")[1].replace(")",""))
        st.session_state['active_user'] = id_pac
        st.session_state['active_user_name'] = paciente_seleccionado.split(" (ID")[0]
        
    st.sidebar.divider()
    
    if 'current_menu' not in st.session_state:
        st.session_state.current_menu = "📊 Dashboard Dietario"

    st.sidebar.markdown("<p style='color:#8E8E93; font-weight:600; font-size:0.9rem; margin-bottom:0.5rem;'>NAVEGACIÓN</p>", unsafe_allow_html=True)
    
    menu_options = ["📊 Dashboard Dietario", "👨‍⚕️ Laboratorios en Sangre", "👤 Ficha de Paciente", "🍽️ Declarar Consumo Diario", "📭 Cola Telegram Externa"]
    
    for option in menu_options:
        if st.sidebar.button(option, use_container_width=True, type="primary" if st.session_state.current_menu == option else "secondary"):
            st.session_state.current_menu = option
            st.rerun()

    st.sidebar.divider()
    if st.sidebar.button("📊 Ver Gráfico Semanal", use_container_width=True):
        if 'active_user' in st.session_state and st.session_state['active_user']:
            mostrar_grafico_semanal(st.session_state['active_user'])
        else:
            st.sidebar.warning("Selecciona un paciente primero.")

    menu = st.session_state.current_menu
    
    # --- SISTEMA DE PESTAÑAS HISTÓRICAS ---
    st.markdown("<p style='color:#8E8E93; font-weight:600; font-size:0.9rem; margin-bottom:0.2rem; margin-top:1rem;'>📅 DÍA DE REGISTRO ACTIVO</p>", unsafe_allow_html=True)
    
    def update_fecha_radio():
        if 'radio_fecha' in st.session_state:
            st.session_state['fecha_activa'] = st.session_state['radio_fecha']
            
    def update_fecha_picker():
        if 'picker_fecha' in st.session_state:
            st.session_state['fecha_activa'] = st.session_state['picker_fecha'].strftime("%Y-%m-%d")

    dias_hist = [(datetime.now() - timedelta(days=i)) for i in range(6, -1, -1)]
    opciones_fecha = {d.strftime("%Y-%m-%d"): d.strftime("%a %d/%m") for d in dias_hist}
    hoy_str = dias_hist[-1].strftime("%Y-%m-%d")
    opciones_fecha[hoy_str] = "Hoy"
    
    if 'fecha_activa' not in st.session_state:
        st.session_state['fecha_activa'] = hoy_str

    if st.session_state['fecha_activa'] not in opciones_fecha:
        opciones_fecha[st.session_state['fecha_activa']] = st.session_state['fecha_activa']
        
    c1, c2 = st.columns([5, 1.5])
    with c1:
        st.radio("Día", list(opciones_fecha.keys()), format_func=lambda x: opciones_fecha[x], horizontal=True, 
                 index=list(opciones_fecha.keys()).index(st.session_state['fecha_activa']), 
                 key="radio_fecha", on_change=update_fecha_radio, label_visibility="collapsed")
    with c2:
        st.date_input("Histórico", value=datetime.strptime(st.session_state['fecha_activa'], "%Y-%m-%d").date(), 
                      key="picker_fecha", on_change=update_fecha_picker, label_visibility="collapsed")
                      
    hoy = st.session_state['fecha_activa']
    # --------------------------------------

    u_id = st.session_state.get('active_user', None)

    # Solo crear log de actividad si hay un usuario seleccionado
    if u_id:
        conn.execute("INSERT INTO ActividadDiaria (id_usuario, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING", (u_id, hoy))
        conn.commit()

    if menu == "📊 Dashboard Dietario":
        if not u_id:
            st.warning("Debe crear o seleccionar un paciente existente en 'Ficha de Paciente' para ver su Dashboard.")
            st.stop()
            
        usuario = conn.execute("SELECT * FROM Usuario WHERE id = ?", (u_id,)).fetchone()
        actividad = conn.execute("SELECT * FROM ActividadDiaria WHERE fecha = ? AND id_usuario=?", (hoy, u_id)).fetchone()
        consumos_df = pd.read_sql_query("SELECT * FROM ConsumoDiario WHERE fecha = ? AND id_usuario=?", conn, params=(hoy, u_id))
        
        cal_consumidas = consumos_df['calorias'].sum() if not consumos_df.empty else 0
        prot_consumidas = consumos_df['proteinas'].sum() if not consumos_df.empty else 0
        carb_consumidas = consumos_df['carbos'].sum() if not consumos_df.empty else 0
        grasas_consumidas = consumos_df['grasas'].sum() if not consumos_df.empty else 0
        
        cal_activas = actividad['calorias_activas'] if actividad else 0
        cal_netas = cal_consumidas - cal_activas
        
        pct_cal = min(cal_consumidas / usuario['obj_calorias'] * 100, 100) if usuario['obj_calorias'] > 0 else 0
        
        # ROW 1: METRICS
        st.markdown("<h2 style='color:#175e4c; margin-bottom:1rem;'>Overview Dashboard</h2>", unsafe_allow_html=True)
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.markdown(f"""
            <div class='light-card-primary'>
                <div class='card-title-primary'>Meta Calórica</div>
                <div class='card-value-primary'>{int(usuario['obj_calorias'])}<span style='font-size:1rem'> kcal</span></div>
                <div class='card-sub-primary'>Objetivo diario ajustado</div>
            </div>
            """, unsafe_allow_html=True)
        with col2:
            st.markdown(f"""
            <div class='light-card'>
                <div class='card-title'>Consumidas</div>
                <div class='card-value' style='color:#d9534f !important;'>-{int(cal_consumidas)}<span style='font-size:1rem'> kcal</span></div>
                <div class='card-sub' style='color:#328b6d !important;'>Ingesta Total</div>
            </div>
            """, unsafe_allow_html=True)
        with col3:
            st.markdown(f"""
            <div class='light-card'>
                <div class='card-title'>Quemadas (Activas)</div>
                <div class='card-value' style='color:#328b6d !important;'>+{int(cal_activas)}<span style='font-size:1rem'> kcal</span></div>
                <div class='card-sub'>Gasto Extra (Apple Watch)</div>
            </div>
            """, unsafe_allow_html=True)
        with col4:
            saldo = (usuario['obj_calorias'] + cal_activas) - cal_consumidas
            
            if saldo >= 0:
                color_saldo = "#175e4c" # Verde
                sub_text = f"Podés comer {int(saldo)} kcal más"
            else:
                color_saldo = "#d9534f" # Rojo
                sub_text = f"Te pasaste por {int(abs(saldo))} kcal"
                
            st.markdown(f"""
            <div class='light-card'>
                <div class='card-title'>Calorías Restantes</div>
                <div class='card-value' style='color:{color_saldo} !important;'>{int(saldo)}<span style='font-size:1rem'> kcal</span></div>
                <div class='card-sub' style='color:{color_saldo}; font-weight:500;'>{sub_text}</div>
            </div>
            """, unsafe_allow_html=True)
            
        # ROW 2: CHARTS
        col_chart_left, col_chart_right = st.columns([1, 1.5])
        
        with col_chart_left:
            st.markdown("<div class='light-card'><div class='card-title'>Progreso Diario (Calorías)</div>", unsafe_allow_html=True)
            
            fig_gauge = go.Figure(go.Indicator(
                mode = "gauge+number",
                value = pct_cal,
                number = {'suffix': "%", 'font': {'color': '#175e4c'}},
                domain = {'x': [0, 1], 'y': [0, 1]},
                gauge = {
                    'axis': {'range': [0, 100], 'visible': False},
                    'bar': {'color': "#328b6d"},
                    'bgcolor': "#e0f2eb",
                    'borderwidth': 0,
                    'shape': "angular"
                }
            ))
            fig_gauge.update_layout(height=250, margin=dict(t=20, b=20, l=10, r=10), paper_bgcolor='rgba(0,0,0,0)', font=dict(color='#222'))
            st.plotly_chart(fig_gauge, use_container_width=True, config={'displayModeBar': False})
            
            st.markdown(f"""
                <div style='display:flex; justify-content:space-between; margin-top:1rem;'>
                    <div><span style='color:#888; font-size:0.8rem;'>Proteínas</span><br><strong style='color:#175e4c;'>{int(prot_consumidas)}g</strong> <span style='font-size:0.7rem;color:#888;'>/ {int(usuario['obj_proteinas'])}g</span></div>
                    <div><span style='color:#888; font-size:0.8rem;'>Carbos</span><br><strong style='color:#328b6d;'>{int(carb_consumidas)}g</strong> <span style='font-size:0.7rem;color:#888;'>/ {int(usuario['obj_carbos'])}g</span></div>
                    <div><span style='color:#888; font-size:0.8rem;'>Grasas</span><br><strong style='color:#85c2a3;'>{int(grasas_consumidas)}g</strong> <span style='font-size:0.7rem;color:#888;'>/ {int(usuario['obj_grasas'])}g</span></div>
                </div>
            </div>""", unsafe_allow_html=True)
            
        with col_chart_right:
            st.markdown("<div class='light-card'><div class='card-title'>Project Analytics (Historial Calorías)</div>", unsafe_allow_html=True)
            
            query_hist_cons = "SELECT fecha, SUM(calorias) as calorias_consumidas, SUM(proteinas) as proteinas, SUM(carbos) as carbos, SUM(grasas) as grasas FROM ConsumoDiario WHERE id_usuario = ? GROUP BY fecha ORDER BY fecha DESC LIMIT 7"
            df_cons = pd.read_sql_query(query_hist_cons, conn, params=(u_id,))
            query_hist_act = "SELECT fecha, calorias_activas FROM ActividadDiaria WHERE id_usuario = ? ORDER BY fecha DESC LIMIT 7"
            df_act = pd.read_sql_query(query_hist_act, conn, params=(u_id,))
            
            if df_cons.empty:
                st.info("No hay datos históricos para graficar.")
            else:
                df_hist = pd.merge(df_cons, df_act, on="fecha", how="left").fillna(0).sort_values("fecha")
                st.plotly_chart(create_weekly_chart(df_hist), use_container_width=True, config={'displayModeBar': False})
            st.markdown("</div>", unsafe_allow_html=True)
            
        # ROW 3: LIST AND MACROS
        col_list, col_macros = st.columns([1.5, 1])
        
        with col_list:
            st.markdown("<div class='light-card'><div class='card-title'>Registros Recientes (Hoy)</div>", unsafe_allow_html=True)
            query_ingestas = "SELECT c.id, b.nombre as Alimento, c.tipo_ingreso as Medida, c.cantidad as Cantidad, c.calorias as Kcal, c.proteinas as Prot, c.carbos as HC, c.grasas as Lip FROM ConsumoDiario c JOIN Biblioteca_Alimentos b ON c.id_alimento = b.id WHERE c.fecha = ? AND c.id_usuario = ? ORDER BY c.id DESC"
            df_hoy = pd.read_sql_query(query_ingestas, conn, params=(hoy, u_id))
            if df_hoy.empty:
                st.info("Sin registros de comida.")
            else:
                df_hoy.insert(0, 'Eliminar', False)
                df_modificado = st.data_editor(df_hoy, hide_index=True, disabled=["id", "Alimento", "Medida", "Cantidad", "Kcal", "Prot", "HC", "Lip"], use_container_width=True)
                if st.button("Borrar Seleccionados", key="dash_borrar"):
                    ids_a_borrar = df_modificado[df_modificado['Eliminar'] == True]['id'].tolist()
                    if ids_a_borrar:
                        placeholders = ','.join(['?'] * len(ids_a_borrar))
                        conn.execute(f"DELETE FROM ConsumoDiario WHERE id IN ({placeholders})", ids_a_borrar)
                        conn.commit()
                        st.success("Borrados.")
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
            
        with col_macros:
            st.markdown("<div class='light-card'><div class='card-title'>Acciones y Suplementación</div>", unsafe_allow_html=True)
            col_sup1, col_sup2 = st.columns(2)
            creatina = col_sup1.checkbox("💊 Creatina (5g)", value=bool(actividad['creatina'] if actividad else False))
            multi = col_sup2.checkbox("🔋 Multivitamínico", value=bool(actividad['multi'] if actividad else False))
            if st.button("Guardar Suplementos", use_container_width=True):
                conn.execute("UPDATE ActividadDiaria SET creatina=?, multi=? WHERE fecha=? AND id_usuario=?", (int(creatina), int(multi), hoy, u_id))
                conn.commit()
                st.rerun()
            
            st.divider()
            new_burn = st.number_input("Calorías Quemadas Extra (Apple Watch):", min_value=0, max_value=8000, value=int(cal_activas), step=10)
            if st.button("Actualizar Gasto", use_container_width=True, type="primary"):
                conn.execute("UPDATE ActividadDiaria SET calorias_activas=? WHERE fecha=? AND id_usuario=?", (new_burn, hoy, u_id))
                conn.commit()
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)


    elif menu == "👨‍⚕️ Laboratorios en Sangre":
        if not u_id:
            st.warning("Crea o selecciona un paciente.")
            st.stop()
            
        st.title(f"Historias Bioquímicas de {st.session_state['active_user_name']}")
        
        tab_subir, tab_analisis, tab_gestion = st.tabs(["Subir Documento Clínico (PDF)", "Evolución Temporal", "Gestionar Historial"])
        
        with tab_subir:
            st.write("Inserta el PDF extraído de la Secretaría de Salud o laboratorios. Trataremos de pescar los marcadores genéricos (Minerales, Vitaminas, Lípidos, Glucosa, etc.).")
            archivo_pdf = st.file_uploader("Subir Laboratorio Extensión PDF", type=["pdf"])
            fecha_lab = st.date_input("Fecha Legal del Análisis")
            
            if archivo_pdf is not None:
                st.info("Escaneando lineas del documento a ciegas...")
                df_detectados = parsear_laboratorio_pdf(archivo_pdf.read())
                
                if isinstance(df_detectados, dict):
                    st.error(f"Falla de Lector: {df_detectados['error']}")
                elif df_detectados.empty:
                    st.warning("No se logró extraer estructuras clásicas de 'Texto -> Número -> Unidad'. Asegúrese de que el PDF sea de texto y no escaneado fotografiado.")
                else:
                    st.success("Hemos mapeado estos marcadores ciegamente. Edita los nombres o borra filas basuras seleccionando la celda antes de sellarlo:")
                    # Interfaz de edicion directa:
                    df_final = st.data_editor(df_detectados, num_rows="dynamic", use_container_width=True)
                    
                    if st.button("Confirmar Firma Electrónica e inyectar al Paciente"):
                        for _, row in df_final.iterrows():
                            # Limpieza extra
                            if pd.notna(row['marcador']) and pd.notna(row['valor']):
                                rmin = float(row['ref_min']) if pd.notna(row.get('ref_min')) else None
                                rmax = float(row['ref_max']) if pd.notna(row.get('ref_max')) else None
                                conn.execute("INSERT INTO AnalisisBioquimicos (id_usuario, fecha, marcador, valor, unidad, ref_min, ref_max) VALUES (?,?,?,?,?,?,?)", 
                                            (u_id, fecha_lab.strftime("%Y-%m-%d"), str(row['marcador']).title(), float(row['valor']), str(row['unidad']), rmin, rmax))
                        conn.commit()
                        st.balloons()
                        st.success("Guardado irreversible completado clínicamente.")

        with tab_analisis:
            st.write("Evolución Lineal en Sangre.")
            marcas_registradas = pd.read_sql_query("SELECT DISTINCT marcador FROM AnalisisBioquimicos WHERE id_usuario = ?", conn, params=(u_id,))
            if marcas_registradas.empty:
                st.warning("No hay marcadores sanguíneos anexados a esta carpeta de paciente.")
            else:
                target_marcadores = st.multiselect("Seleccione los indicadores a monitorear:", marcas_registradas['marcador'].tolist(), default=marcas_registradas['marcador'].tolist()[:1])
                for target_marcador in target_marcadores:
                    # Traer datos temporales
                    datos_marc = pd.read_sql_query("SELECT fecha, valor, unidad, ref_min, ref_max FROM AnalisisBioquimicos WHERE id_usuario = ? AND marcador = ? ORDER BY fecha ASC", conn, params=(u_id, target_marcador))
                    
                    if not datos_marc.empty:
                        st.write(f"Variación temporal de **{target_marcador}** (Medido en `{datos_marc.iloc[0]['unidad']}`)")
                        fig2 = go.Figure()
                        
                        v_mins = datos_marc['ref_min'].dropna()
                        v_maxs = datos_marc['ref_max'].dropna()
                        if not v_mins.empty and not v_maxs.empty:
                            fig2.add_hrect(y0=v_mins.mean(), y1=v_maxs.mean(), line_width=0, fillcolor="#B4D330", opacity=0.15, layer="below")
                            
                        colores = []
                        textos = []
                        for i, r in datos_marc.iterrows():
                            es_anom = False
                            if pd.notna(r['ref_min']) and r['valor'] < r['ref_min']: es_anom = True
                            if pd.notna(r['ref_max']) and r['valor'] > r['ref_max']: es_anom = True
                            colores.append('#d9534f' if es_anom else '#3C8E86')
                            textos.append(f"<b>{r['valor']}</b><br>{r['unidad']}")
                            
                        fechas_short = pd.to_datetime(datos_marc['fecha']).dt.strftime('%d/%m/%Y')
                        
                        fig2.add_trace(go.Bar(
                            x=fechas_short, 
                            y=datos_marc["valor"], 
                            marker_color=colores,
                            width=0.35,
                            marker_line_width=0,
                            text=textos,
                            textposition='outside',
                            textfont=dict(size=12, color='#222')
                        ))
                        
                        fig2.update_layout(
                            paper_bgcolor='rgba(0,0,0,0)', 
                            plot_bgcolor='rgba(0,0,0,0)', 
                            margin=dict(t=50, b=20, l=10, r=10), 
                            height=380,
                            xaxis=dict(showgrid=False, zeroline=False, tickfont=dict(size=13, color='#555')), 
                            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                            showlegend=False
                        )
                        
                        max_y = datos_marc["valor"].max() * 1.25
                        if max_y > 0: fig2.update_yaxes(range=[0, max_y])
                        
                        st.plotly_chart(fig2, use_container_width=True, config={'displayModeBar': False})

        with tab_gestion:
            st.write("Borrado de Archivos Clínicos y Filas Basuras.")
            datos_completos = pd.read_sql_query("SELECT id, fecha, marcador, valor, unidad, ref_min, ref_max FROM AnalisisBioquimicos WHERE id_usuario = ? ORDER BY fecha DESC", conn, params=(u_id,))
            if datos_completos.empty:
                st.info("No hay registros clínicos en la base de datos para este paciente.")
            else:
                datos_completos.insert(0, 'Eliminar', False)
                
                def highlight_anomalies(row):
                    color = [''] * len(row)
                    val = row['valor']
                    try:
                        rmin = float(row['ref_min']) if pd.notna(row['ref_min']) else None
                        rmax = float(row['ref_max']) if pd.notna(row['ref_max']) else None
                        if (rmin is not None and val < rmin) or (rmax is not None and val > rmax):
                            idx = row.index.get_loc('valor')
                            color[idx] = 'background-color: #ffcccc; color: #990000; font-weight: bold;'
                    except: pass
                    return color
                
                styled_df = datos_completos.style.apply(highlight_anomalies, axis=1)
                
                datos_modificados = st.data_editor(
                    styled_df,
                    hide_index=True,
                    disabled=["id", "fecha", "marcador", "valor", "unidad"],
                    use_container_width=True
                )
                
                if st.button("Aplicar Purga de Seleccionados", type="primary"):
                    ids_to_delete = datos_modificados[datos_modificados['Eliminar'] == True]['id'].tolist()
                    if ids_to_delete:
                        placeholders = ','.join(['?'] * len(ids_to_delete))
                        conn.execute(f"DELETE FROM AnalisisBioquimicos WHERE id IN ({placeholders})", ids_to_delete)
                        conn.commit()
                        st.success(f"¡{len(ids_to_delete)} registros eliminados con éxito!")
                        st.rerun()
                    else:
                        st.warning("No marcaste ningún registro para eliminar.")

    elif menu == "👤 Ficha de Paciente":
        st.title("Adminstración Física (Ecuación Mifflin-St Jeor)")
        
        operacion = st.radio("Acción a realizar:", ["Visualizar / Editar Ficha Actual", "Dar de Alta Perfil Nuevo"])
        usuario = conn.execute("SELECT * FROM Usuario WHERE id = ?", (u_id,)).fetchone() if u_id and operacion == "Visualizar / Editar Ficha Actual" else None
        
        with st.form("perfil_clinico_form"):
            col1, col2 = st.columns(2)
            nombre = col1.text_input("Nombre / Referencia de Historia Clínica", value=usuario['nombre'] if usuario else "")
            edad = col2.number_input("Edad Real (Años)", min_value=12, max_value=120, value=usuario['edad'] if usuario else 30)
            
            telegram_id = st.text_input("ID de Telegram del Paciente (Requerido para enlazar el Bot)", value=usuario['telegram_id'] if usuario and 'telegram_id' in usuario and usuario['telegram_id'] else "")
            
            c1, c2, c3 = st.columns(3)
            peso = c1.number_input("Báscula Clínica (kg)", min_value=30.0, value=usuario['peso'] if usuario else 75.0)
            altura = c2.number_input("Estatura (cm)", min_value=100.0, value=usuario['altura'] if usuario else 175.0)
            grasa = c3.number_input("Grasa Corporal (%)", min_value=0.0, max_value=80.0, value=usuario['grasa_corporal'] if usuario else 20.0)
            
            sexo = st.radio("Morfología Estructural", ["Hombre", "Mujer"], index=0 if (not usuario or usuario['sexo'] == 'Hombre') else 1)
            
            naf_options = ["Sedentario", "Ligero", "Moderado", "Intenso"]
            def_idx = 0
            if usuario and usuario['nivel_actividad'] in naf_options:
                def_idx = naf_options.index(usuario['nivel_actividad'])
            nivel_actividad = st.selectbox("Factor de Actividad Física", naf_options, index=def_idx)
            
            st.divider()
            st.caption("Al Guardar Ficha se procesará automáticamente el sistema Recompositor Corporal -15% sobre las de Base Reposo.")
            
            if st.form_submit_button("Someter Cálculos Recompositivos y Guardar"):
                def calcular_metas(peso, altura, edad, genero, nivel_actividad):
                    # 1. Cálculo del TMB
                    if genero == "Hombre":
                        tmb = (10 * peso) + (6.25 * altura) - (5 * edad) + 5
                    else:
                        tmb = (10 * peso) + (6.25 * altura) - (5 * edad) - 161
                        
                    # 2. Multiplicador por actividad física
                    naf = {
                        "Sedentario": 1.2,
                        "Ligero": 1.375,
                        "Moderado": 1.55,
                        "Intenso": 1.725
                    }
                    get = tmb * naf.get(nivel_actividad, 1.2)
                    
                    # 3. Ajuste para RECOMPOSICIÓN (Bajar grasa y subir músculo)
                    meta_proteina = peso * 2.2
                    objetivo_kcal = get * 0.85 
                    
                    return {
                        "tmb": round(tmb),
                        "get": round(get),
                        "objetivo_kcal": round(objetivo_kcal),
                        "meta_proteina": round(meta_proteina)
                    }

                metas = calcular_metas(peso, altura, edad, sexo, nivel_actividad)
                tmb = metas['tmb']
                o_cal = metas['objetivo_kcal']
                o_pro = metas['meta_proteina']
                
                # Completar macros restantes para la DB
                o_gra = round(peso * 0.8)
                o_car = round(max(0, (o_cal - (o_pro * 4) - (o_gra * 9)) / 4.0))
                
                if operacion == "Dar de Alta Perfil Nuevo":
                    conn.execute("""INSERT INTO Usuario (telegram_id, nombre, edad, peso, altura, sexo, nivel_actividad, grasa_corporal, tmb, obj_calorias, obj_proteinas, obj_carbos, obj_grasas)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (telegram_id, nombre, edad, peso, altura, sexo, nivel_actividad, grasa, tmb, o_cal, o_pro, o_car, o_gra))
                    conn.commit()
                    st.success(f"¡Alta Concedida! Carga exitosa. Refresca la ventana para seleccionarlo arriba al costado. (TMB Calculada: {tmb:.0f})")
                else:
                    if u_id:
                        conn.execute("""UPDATE Usuario SET telegram_id=?, nombre=?, edad=?, peso=?, altura=?, sexo=?, nivel_actividad=?, grasa_corporal=?, tmb=?, obj_calorias=?, obj_proteinas=?, obj_carbos=?, obj_grasas=? WHERE id=?""",
                                     (telegram_id, nombre, edad, peso, altura, sexo, nivel_actividad, grasa, tmb, o_cal, o_pro, o_car, o_gra, u_id))
                        conn.commit()
                        st.success("Modificación Operada. Dashboard Dietario Actualizado.")

        if u_id and operacion == "Visualizar / Editar Ficha Actual":
            st.divider()
            with st.expander("⚠️ Zona de Peligro - Borrado de Historial Clínico"):
                st.error("Al purgar esta Ficha, destruirás todas las ingestas, métricas bioquímicas, historiales y la Autorización de Telegram de forma irreversible.")
                if st.button("Destruir Ficha Médica Permanentemente", type="primary"):
                    conn.execute("DELETE FROM Usuario WHERE id = ?", (u_id,))
                    conn.execute("DELETE FROM ConsumoDiario WHERE id_usuario = ?", (u_id,))
                    conn.execute("DELETE FROM ActividadDiaria WHERE id_usuario = ?", (u_id,))
                    conn.execute("DELETE FROM AnalisisBioquimicos WHERE id_usuario = ?", (u_id,))
                    conn.execute("DELETE FROM Pendientes WHERE id_usuario = ?", (u_id,))
                    conn.commit()
                    st.session_state['active_user'] = None
                    if 'active_user_name' in st.session_state: del st.session_state['active_user_name']
                    st.toast("Expediente completamente purgado.")
                    st.rerun()


    elif menu == "🍽️ Declarar Consumo Diario":
        if not u_id:
            st.warning("Especifique un paciente activo."); st.stop()
            
        st.title("Bitácora de Consumo Alimentario")
        
        tab_local, tab_off, tab_nuevo = st.tabs(["Anotar de Base Local", "Buscar en OpenFoodFacts", "➕ Crear Alimento Manual"])
        
        with tab_local:
            alimentos = pd.read_sql_query("SELECT id, nombre, porcion_base_g FROM Biblioteca_Alimentos ORDER BY nombre", conn)
            
            if alimentos.empty:
                st.warning("Biblioteca OFF Carece de Registros.")
            else:
                opcion = st.selectbox("Directorio Local Comida:", alimentos['nombre'].tolist())
                id_alim = alimentos.loc[alimentos['nombre'] == opcion, 'id'].values[0]
                porcion_g = alimentos.loc[alimentos['nombre'] == opcion, 'porcion_base_g'].values[0]
                
                tipo_ingreso = st.radio("Forma de Medición:", ["⚖️ En Gramos", f"🥐 En Porciones/Unidades (1 unidad = {porcion_g}g)"])
                
                if "Gramos" in tipo_ingreso:
                    cantidad = st.number_input("Peso consumido (g):", value=100.0)
                else:
                    cantidad = st.number_input("Cantidad de Unidades/Porciones:", value=1.0, step=0.25)
                
                if st.button("Guardar Consumo"):
                    al = conn.execute("SELECT * FROM Biblioteca_Alimentos WHERE id=?", (int(id_alim),)).fetchone()
                    if "Gramos" in tipo_ingreso: f = cantidad / 100.0; cal, p, c, g = al['cal_100g']*f, al['prot_100g']*f, al['carb_100g']*f, al['grasas_100g']*f
                    else: cal, p, c, g = al['cal_porcion']*cantidad, al['prot_porcion']*cantidad, al['carb_porcion']*cantidad, al['grasas_porcion']*cantidad
                        
                    conn.execute("""INSERT INTO ConsumoDiario 
                                 (id_usuario, fecha, id_alimento, tipo_ingreso, cantidad, calorias, proteinas, carbos, grasas)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (u_id, hoy, int(id_alim), tipo_ingreso, cantidad, cal, p, c, g))
                    conn.commit()
                    st.success("Trazado con Éxito. Restado de los saldos generales del paciente.")

        with tab_nuevo:
            st.markdown("<h2>Crear Nuevo Alimento en Base Local</h2>", unsafe_allow_html=True)
            st.info("Ingresa los valores nutricionales por porción. Se guardará permanentemente en tu biblioteca personal.")
            with st.form("nuevo_alimento_manual_form"):
                n_nombre = st.text_input("Nombre del Alimento (Ej: Medialuna de Manteca)")
                
                tipo_porcion = st.radio("¿Cómo sueles medirlo?", ["Por Unidades/Piezas (Ej: 1 medialuna)", "Por Peso Estándar (Ej: 100g, 250g)"])
                
                if "Unidades" in tipo_porcion:
                    n_porcion = st.number_input("Peso estimado de 1 unidad (en gramos)", min_value=1.0, value=50.0, help="Si no sabes el peso exacto, deja el valor aproximado.")
                    st.caption("A continuación, ingresa los macronutrientes que aporta **1 sola unidad**.")
                else:
                    n_porcion = st.number_input("Tamaño de la Porción (en gramos)", min_value=1.0, value=100.0)
                    st.caption(f"A continuación, ingresa los macronutrientes que aportan **{int(n_porcion)}g** de este alimento.")
                
                c1, c2, c3, c4 = st.columns(4)
                n_cal = c1.number_input("Calorías", min_value=0.0)
                n_pro = c2.number_input("Proteínas (g)", min_value=0.0)
                n_car = c3.number_input("Carbos (g)", min_value=0.0)
                n_gra = c4.number_input("Grasas (g)", min_value=0.0)
                
                if st.form_submit_button("Guardar en Biblioteca", type="primary"):
                    if not n_nombre.strip():
                        st.error("El nombre no puede estar vacío.")
                    else:
                        # Calcular valores por cada 100g para estandarización interna
                        factor_100 = 100.0 / n_porcion
                        conn.execute("""INSERT INTO Biblioteca_Alimentos 
                                     (nombre, porcion_base_g, cal_porcion, prot_porcion, carb_porcion, grasas_porcion,
                                      cal_100g, prot_100g, carb_100g, grasas_100g)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                     (n_nombre.strip().title(), n_porcion, n_cal, n_pro, n_car, n_gra,
                                      n_cal * factor_100, n_pro * factor_100, n_car * factor_100, n_gra * factor_100))
                        conn.commit()
                        st.success(f"¡{n_nombre.title()} agregado a tu base de datos exitosamente! Ahora puedes seleccionarlo en la primera pestaña.")
                        st.rerun()

        with tab_off:
            st.markdown("<h2>Base Biológica (OpenFoodFacts Mundial)</h2>", unsafe_allow_html=True)
            termino = st.text_input("Ingresá el Nombre del ítem global/Código:")
            if st.button("Buscar Nube Pública Centralizada"):
                st.session_state['off_results'] = buscar_alimento_off_api(termino)
                
            if 'off_results' in st.session_state:
                res = st.session_state['off_results']
                if isinstance(res, dict) and "error" in res: st.error(res["error"])
                elif not res: st.warning("Ítem Vacío en el Banco.")
                else:
                    opciones = [f"{p.get('brands', 'N/D')} | {p.get('product_name', 'Borroso')} (OFF: {p.get('_id', '')})" for p in res]
                    elegido = st.selectbox("Catálogo Recuperado:", opciones)
                    
                    if elegido:
                        idx = opciones.index(elegido)
                        p = res[idx]
                        nt = p.get('nutriments', {})
                        serving = p.get('serving_size', '')
                        pd_d = 100.0
                        if serving:
                            mm = re.search(r'(\d+[\.,]?\d*)', serving)
                            if mm: pd_d = float(mm.group(1).replace(',','.'))
                        
                        with st.form("off_guardar_form"):
                            off_nombre = st.text_input("Alias Interno a Guardar:", value=f"{p.get('brands', '')} {p.get('product_name', '')}".strip())
                            f_porc = st.number_input("Serving (Pesa de Porción Estándar)", value=pd_d)
                            c1, c2 = st.columns(2)
                            f_cal = c1.number_input("Kcal x100g", value=float(nt.get('energy-kcal_100g') or 0.0))
                            f_prot = c2.number_input("Proteínas x100g", value=float(nt.get('proteins_100g') or 0.0))
                            f_carb = c1.number_input("HC x100g", value=float(nt.get('carbohydrates_100g') or 0.0))
                            f_grasas = c2.number_input("Lípidos Totales x100g", value=float(nt.get('fat_100g') or 0.0))
                            
                            if st.form_submit_button("Exportar Renglon a Ficha Global Local"):
                                f = f_porc / 100.0
                                conn.execute("""INSERT INTO Biblioteca_Alimentos 
                                    (nombre, porcion_base_g, cal_100g, prot_100g, carb_100g, grasas_100g, cal_porcion, prot_porcion, carb_porcion, grasas_porcion)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                                    (off_nombre, f_porc, f_cal, f_prot, f_carb, f_grasas, f_cal*f, f_prot*f, f_carb*f, f_grasas*f))
                                conn.commit()
                                st.success("Guardado Permanentemente fuera de RED externa.")

        st.markdown("<br><div class='apple-title'>Historial de Consumos del Día</div>", unsafe_allow_html=True)
        query_ingestas = "SELECT c.id, b.nombre as Alimento, c.tipo_ingreso as Medida, c.cantidad as Cantidad, c.calorias as Kcal, c.proteinas as Prot, c.carbos as HC, c.grasas as Lip FROM ConsumoDiario c JOIN Biblioteca_Alimentos b ON c.id_alimento = b.id WHERE c.fecha = ? AND c.id_usuario = ? ORDER BY c.id DESC"
        df_hoy = pd.read_sql_query(query_ingestas, conn, params=(hoy, u_id))
        if df_hoy.empty:
            st.info("Aún no has registrado consumos hoy.")
        else:
            df_hoy.insert(0, 'Eliminar', False)
            df_modificado = st.data_editor(df_hoy, hide_index=True, disabled=["id", "Alimento", "Medida", "Cantidad", "Kcal", "Prot", "HC", "Lip"], use_container_width=True, key="editor_consumos_dia")
            if st.button("Borrar Consumos Seleccionados", key="btn_borrar_consumos"):
                ids_a_borrar = df_modificado[df_modificado['Eliminar'] == True]['id'].tolist()
                if ids_a_borrar:
                    placeholders = ','.join(['?'] * len(ids_a_borrar))
                    conn.execute(f"DELETE FROM ConsumoDiario WHERE id IN ({placeholders})", ids_a_borrar)
                    conn.commit()
                    st.success(f"{len(ids_a_borrar)} consumos eliminados.")
                    st.rerun()
                else:
                    st.warning("No marcaste ningún cuadro para eliminar.")

    elif menu == "📭 Cola Telegram Externa":
        if not u_id:
            st.warning("Seleccioná paciente en el menú lateral porque esta acción impactará en SU cuenta bancaria calórica."); st.stop()
            
        st.title(f"Recepción de Logs Diarios -> Enviando a Ficha de {st.session_state['active_user_name']}")
        pendientes = pd.read_sql_query("SELECT * FROM Pendientes WHERE procesado = 0", conn)
        
        if pendientes.empty: st.write("Servidor Local despejado. No han entrado notificaciones del teléfono.")
        else:
            for _, row in pendientes.iterrows():
                with st.expander(f"📥 Cola: {row['fecha']} - {'📷 Biométrico (Watch)' if row['tipo'] == 'imagen' else '💬 Texto Oral'}"):
                    if row['tipo'] == 'texto':
                        st.write(f"Voz Extraída: `{row['contenido']}`")
                        if st.button(f"🔎 Enviar a Base Mundial OFF (Autómatizado)", key=f"abn_{row['id']}"):
                            st.session_state['off_results'] = buscar_alimento_off_api(row['contenido'])
                            st.info("Query Enviada. Continúe en panel de Banco OFF.")
                    elif row['tipo'] == 'imagen':
                        if os.path.exists(row['contenido']):
                            st.image(row['contenido'], width=400)
                            if st.button("Tratamiento OCR sobre Sangría Diaria", key=f"ocr_{row['id']}"):
                                stat = extract_calories_ocr(row['contenido'])
                                if isinstance(stat, float):
                                    st.success(f"Inyectando Actividad ({stat} Kal) a {st.session_state['active_user_name']}.")
                                    conn.execute("INSERT INTO ActividadDiaria (id_usuario, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING", (u_id, hoy))
                                    conn.execute("UPDATE ActividadDiaria SET calorias_activas = calorias_activas + ? WHERE fecha = ? AND id_usuario=?", (stat, hoy, u_id))
                                    conn.execute("UPDATE Pendientes SET procesado = 1 WHERE id = ?", (int(row['id']),))
                                    conn.commit()
                                    st.rerun()
                                elif isinstance(stat, dict) and stat:
                                    total_kcal = sum(stat.values())
                                    st.success(f"Inyectando Actividad Multi-día ({total_kcal} kcal) a {st.session_state['active_user_name']}.")
                                    for d_str, cals in stat.items():
                                        conn.execute("INSERT INTO ActividadDiaria (id_usuario, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING", (u_id, d_str))
                                        conn.execute("UPDATE ActividadDiaria SET calorias_activas = calorias_activas + ? WHERE fecha = ? AND id_usuario=?", (cals, d_str, u_id))
                                    conn.execute("UPDATE Pendientes SET procesado = 1 WHERE id = ?", (int(row['id']),))
                                    conn.commit()
                                    st.rerun()
                                else: st.error(str(stat) if not isinstance(stat, dict) else "No se detectaron calorías válidas.")
                        else: st.warning("Archivo Caché Eliminado por el sistema host.")
                            
                    if st.button("Depurar Fila de Base Aislada", key=f"del_{row['id']}"):
                        conn.execute("UPDATE Pendientes SET procesado = 1 WHERE id = ?", (int(row['id']),))
                        conn.commit()
                        st.rerun()

    conn.close()

if __name__ == "__main__":
    main()
