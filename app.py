import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import threading
import logging

from database import get_db_connection, upsert_actividad, delete_selected_rows, get_weekly_data, calcular_metas
from telegram_bot import start_telebot_thread
from parsers import extract_calories_ocr, parsear_laboratorio_pdf, buscar_alimento_off_api
from ui_components import DASHBOARD_CSS, create_weekly_chart, create_lab_chart

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Mi Nutri App Clínica", layout="wide", page_icon="🧬")
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

# Iniciar Thread del Bot (idempotente)
if 'tg_thread' not in st.session_state:
    threading.Thread(target=start_telebot_thread, daemon=True).start()
    st.session_state['tg_thread'] = True

def main():
    try:
        with get_db_connection() as conn:
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
                
                old_idx = 0
                if 'active_user' in st.session_state and st.session_state['active_user']:
                    target_str = f"{st.session_state['active_user_name']} (ID: {st.session_state['active_user']})"
                    if target_str in opciones_p: 
                        old_idx = opciones_p.index(target_str)
                    
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
                    mostrar_grafico_semanal(st.session_state['active_user'], conn)
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
            u_id = st.session_state.get('active_user', None)

            if u_id:
                upsert_actividad(conn, u_id, hoy)
                conn.commit()

            # --- ROUTING DE VISTAS ---
            if menu == "📊 Dashboard Dietario":
                render_dashboard(conn, u_id, hoy)
            elif menu == "👨‍⚕️ Laboratorios en Sangre":
                render_laboratorios(conn, u_id)
            elif menu == "👤 Ficha de Paciente":
                render_ficha(conn, u_id)
            elif menu == "🍽️ Declarar Consumo Diario":
                render_consumo(conn, u_id, hoy)
            elif menu == "📭 Cola Telegram Externa":
                render_cola(conn, u_id, hoy)
                
    except Exception as e:
        logger.error(f"Error crítico en main app loop: {e}", exc_info=True)
        st.error(f"Se produjo un error al cargar la aplicación: {e}")

@st.dialog("📊 Balance Semanal de Calorías (Últimos 7 días)")
def mostrar_grafico_semanal(u_id, conn):
    df_cons, df_act = get_weekly_data(conn, u_id)
    if df_cons.empty:
        st.info("No hay datos históricos para graficar.")
    else:
        df_hist = pd.merge(df_cons, df_act, on="fecha", how="left").fillna(0).sort_values("fecha")
        st.plotly_chart(create_weekly_chart(df_hist), use_container_width=True, config={'displayModeBar': False})

def render_dashboard(conn, u_id, hoy):
    if not u_id:
        st.warning("Debe crear o seleccionar un paciente existente en 'Ficha de Paciente' para ver su Dashboard.")
        return
        
    usuario = conn.execute("SELECT * FROM Usuario WHERE id = ?", (u_id,)).fetchone()
    actividad = conn.execute("SELECT * FROM ActividadDiaria WHERE fecha = ? AND id_usuario=?", (hoy, u_id)).fetchone()
    consumos_df = pd.read_sql_query("SELECT * FROM ConsumoDiario WHERE fecha = ? AND id_usuario=?", conn, params=(hoy, u_id))
    
    cal_consumidas = consumos_df['calorias'].sum() if not consumos_df.empty else 0
    prot_consumidas = consumos_df['proteinas'].sum() if not consumos_df.empty else 0
    carb_consumidas = consumos_df['carbos'].sum() if not consumos_df.empty else 0
    grasas_consumidas = consumos_df['grasas'].sum() if not consumos_df.empty else 0
    
    cal_activas = actividad['calorias_activas'] if actividad else 0
    saldo = (usuario['obj_calorias'] + cal_activas) - cal_consumidas
    pct_cal = min(cal_consumidas / usuario['obj_calorias'] * 100, 100) if usuario['obj_calorias'] > 0 else 0
    
    st.markdown("<h2 style='color:#175e4c; margin-bottom:1rem;'>Overview Dashboard</h2>", unsafe_allow_html=True)
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown(f"<div class='light-card-primary'><div class='card-title-primary'>Meta Calórica</div><div class='card-value-primary'>{int(usuario['obj_calorias'])}<span style='font-size:1rem'> kcal</span></div><div class='card-sub-primary'>Objetivo diario ajustado</div></div>", unsafe_allow_html=True)
    with col2:
        st.markdown(f"<div class='light-card'><div class='card-title'>Consumidas</div><div class='card-value' style='color:#d9534f !important;'>-{int(cal_consumidas)}<span style='font-size:1rem'> kcal</span></div><div class='card-sub' style='color:#328b6d !important;'>Ingesta Total</div></div>", unsafe_allow_html=True)
    with col3:
        st.markdown(f"<div class='light-card'><div class='card-title'>Quemadas (Activas)</div><div class='card-value' style='color:#328b6d !important;'>+{int(cal_activas)}<span style='font-size:1rem'> kcal</span></div><div class='card-sub'>Gasto Extra (Apple Watch)</div></div>", unsafe_allow_html=True)
    with col4:
        color_saldo = "#175e4c" if saldo >= 0 else "#d9534f"
        sub_text = f"Podés comer {int(saldo)} kcal más" if saldo >= 0 else f"Te pasaste por {int(abs(saldo))} kcal"
        st.markdown(f"<div class='light-card'><div class='card-title'>Calorías Restantes</div><div class='card-value' style='color:{color_saldo} !important;'>{int(saldo)}<span style='font-size:1rem'> kcal</span></div><div class='card-sub' style='color:{color_saldo}; font-weight:500;'>{sub_text}</div></div>", unsafe_allow_html=True)
        
    col_chart_left, col_chart_right = st.columns([1, 1.5])
    
    with col_chart_left:
        st.markdown("<div class='light-card'><div class='card-title'>Progreso Diario (Calorías)</div>", unsafe_allow_html=True)
        import plotly.graph_objects as go
        fig_gauge = go.Figure(go.Indicator(
            mode = "gauge+number", value = pct_cal,
            number = {'suffix': "%", 'font': {'color': '#175e4c'}},
            domain = {'x': [0, 1], 'y': [0, 1]},
            gauge = {'axis': {'range': [0, 100], 'visible': False}, 'bar': {'color': "#328b6d"}, 'bgcolor': "#e0f2eb", 'borderwidth': 0, 'shape': "angular"}
        ))
        fig_gauge.update_layout(height=250, margin=dict(t=20, b=20, l=10, r=10), paper_bgcolor='rgba(0,0,0,0)', font=dict(color='#222'))
        st.plotly_chart(fig_gauge, use_container_width=True, config={'displayModeBar': False})
        
        st.markdown(f"<div style='display:flex; justify-content:space-between; margin-top:1rem;'><div><span style='color:#888; font-size:0.8rem;'>Proteínas</span><br><strong style='color:#175e4c;'>{int(prot_consumidas)}g</strong> <span style='font-size:0.7rem;color:#888;'>/ {int(usuario['obj_proteinas'])}g</span></div><div><span style='color:#888; font-size:0.8rem;'>Carbos</span><br><strong style='color:#328b6d;'>{int(carb_consumidas)}g</strong> <span style='font-size:0.7rem;color:#888;'>/ {int(usuario['obj_carbos'])}g</span></div><div><span style='color:#888; font-size:0.8rem;'>Grasas</span><br><strong style='color:#85c2a3;'>{int(grasas_consumidas)}g</strong> <span style='font-size:0.7rem;color:#888;'>/ {int(usuario['obj_grasas'])}g</span></div></div></div>", unsafe_allow_html=True)
        
    with col_chart_right:
        st.markdown("<div class='light-card'><div class='card-title'>Project Analytics (Historial Calorías)</div>", unsafe_allow_html=True)
        df_cons, df_act = get_weekly_data(conn, u_id)
        if df_cons.empty:
            st.info("No hay datos históricos para graficar.")
        else:
            df_hist = pd.merge(df_cons, df_act, on="fecha", how="left").fillna(0).sort_values("fecha")
            st.plotly_chart(create_weekly_chart(df_hist), use_container_width=True, config={'displayModeBar': False})
        st.markdown("</div>", unsafe_allow_html=True)
        
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
                    delete_selected_rows(conn, "ConsumoDiario", ids_a_borrar)
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

def render_laboratorios(conn, u_id):
    if not u_id:
        st.warning("Crea o selecciona un paciente.")
        return
        
    st.title(f"Historias Bioquímicas de {st.session_state['active_user_name']}")
    
    tab_subir, tab_analisis, tab_gestion = st.tabs(["Subir Documento Clínico (PDF)", "Evolución Temporal", "Gestionar Historial"])
    
    with tab_subir:
        st.write("Inserta el PDF extraído de la Secretaría de Salud o laboratorios. Trataremos de pescar los marcadores genéricos.")
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
                df_final = st.data_editor(df_detectados, num_rows="dynamic", use_container_width=True)
                
                if st.button("Confirmar Firma Electrónica e inyectar al Paciente"):
                    for _, row in df_final.iterrows():
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
                datos_marc = pd.read_sql_query("SELECT fecha, valor, unidad, ref_min, ref_max FROM AnalisisBioquimicos WHERE id_usuario = ? AND marcador = ? ORDER BY fecha ASC", conn, params=(u_id, target_marcador))
                
                if not datos_marc.empty:
                    st.write(f"Variación temporal de **{target_marcador}** (Medido en `{datos_marc.iloc[0]['unidad']}`)")
                    st.plotly_chart(create_lab_chart(datos_marc), use_container_width=True, config={'displayModeBar': False})

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
            datos_modificados = st.data_editor(styled_df, hide_index=True, disabled=["id", "fecha", "marcador", "valor", "unidad"], use_container_width=True)
            
            if st.button("Aplicar Purga de Seleccionados", type="primary"):
                ids_to_delete = datos_modificados[datos_modificados['Eliminar'] == True]['id'].tolist()
                if ids_to_delete:
                    delete_selected_rows(conn, "AnalisisBioquimicos", ids_to_delete)
                    st.success(f"¡{len(ids_to_delete)} registros eliminados con éxito!")
                    st.rerun()

def render_ficha(conn, u_id):
    st.title("Adminstración Física (Ecuación Mifflin-St Jeor)")
    
    operacion = st.radio("Acción a realizar:", ["Visualizar / Editar Ficha Actual", "Dar de Alta Perfil Nuevo"])
    usuario = conn.execute("SELECT * FROM Usuario WHERE id = ?", (u_id,)).fetchone() if u_id and operacion == "Visualizar / Editar Ficha Actual" else None
    
    with st.form("perfil_clinico_form"):
        col1, col2 = st.columns(2)
        nombre = col1.text_input("Nombre / Referencia de Historia Clínica", value=usuario['nombre'] if usuario else "")
        edad = col2.number_input("Edad Real (Años)", min_value=12, max_value=120, value=usuario['edad'] if usuario else 30)
        
        telegram_id = st.text_input("ID de Telegram del Paciente (Requerido para enlazar el Bot)", value=usuario.get('telegram_id', '') if usuario else "")
        
        c1, c2, c3 = st.columns(3)
        peso = c1.number_input("Báscula Clínica (kg)", min_value=30.0, value=usuario['peso'] if usuario else 75.0)
        altura = c2.number_input("Estatura (cm)", min_value=100.0, value=usuario['altura'] if usuario else 175.0)
        grasa = c3.number_input("Grasa Corporal (%)", min_value=0.0, max_value=80.0, value=usuario['grasa_corporal'] if usuario else 20.0)
        
        sexo = st.radio("Morfología Estructural", ["Hombre", "Mujer"], index=0 if (not usuario or usuario['sexo'] == 'Hombre') else 1)
        
        naf_options = ["Sedentario", "Ligero", "Moderado", "Intenso"]
        def_idx = naf_options.index(usuario['nivel_actividad']) if usuario and usuario['nivel_actividad'] in naf_options else 0
        nivel_actividad = st.selectbox("Factor de Actividad Física", naf_options, index=def_idx)
        
        st.divider()
        st.caption("Al Guardar Ficha se procesará automáticamente el sistema Recompositor Corporal -15% sobre las de Base Reposo.")
        
        if st.form_submit_button("Someter Cálculos Recompositivos y Guardar"):
            metas = calcular_metas(peso, altura, edad, sexo, nivel_actividad)
            tmb, o_cal, o_pro = metas['tmb'], metas['objetivo_kcal'], metas['meta_proteina']
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

def render_consumo(conn, u_id, hoy):
    if not u_id:
        st.warning("Especifique un paciente activo."); return
        
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
                if "Gramos" in tipo_ingreso: 
                    f = cantidad / 100.0; cal, p, c, g = al['cal_100g']*f, al['prot_100g']*f, al['carb_100g']*f, al['grasas_100g']*f
                else: 
                    cal, p, c, g = al['cal_porcion']*cantidad, al['prot_porcion']*cantidad, al['carb_porcion']*cantidad, al['grasas_porcion']*cantidad
                    
                conn.execute("""INSERT INTO ConsumoDiario (id_usuario, fecha, id_alimento, tipo_ingreso, cantidad, calorias, proteinas, carbos, grasas) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (u_id, hoy, int(id_alim), tipo_ingreso, cantidad, cal, p, c, g))
                conn.commit()
                st.success("Trazado con Éxito. Restado de los saldos generales del paciente.")

    with tab_nuevo:
        st.markdown("<h2>Crear Nuevo Alimento en Base Local</h2>", unsafe_allow_html=True)
        with st.form("nuevo_alimento_manual_form"):
            n_nombre = st.text_input("Nombre del Alimento (Ej: Medialuna de Manteca)")
            tipo_porcion = st.radio("¿Cómo sueles medirlo?", ["Por Unidades/Piezas (Ej: 1 medialuna)", "Por Peso Estándar (Ej: 100g, 250g)"])
            
            if "Unidades" in tipo_porcion:
                n_porcion = st.number_input("Peso estimado de 1 unidad (en gramos)", min_value=1.0, value=50.0)
            else:
                n_porcion = st.number_input("Tamaño de la Porción (en gramos)", min_value=1.0, value=100.0)
            
            c1, c2, c3, c4 = st.columns(4)
            n_cal = c1.number_input("Calorías", min_value=0.0)
            n_pro = c2.number_input("Proteínas (g)", min_value=0.0)
            n_car = c3.number_input("Carbos (g)", min_value=0.0)
            n_gra = c4.number_input("Grasas (g)", min_value=0.0)
            
            if st.form_submit_button("Guardar en Biblioteca", type="primary"):
                if not n_nombre.strip():
                    st.error("El nombre no puede estar vacío.")
                else:
                    factor_100 = 100.0 / n_porcion
                    conn.execute("""INSERT INTO Biblioteca_Alimentos (nombre, porcion_base_g, cal_porcion, prot_porcion, carb_porcion, grasas_porcion, cal_100g, prot_100g, carb_100g, grasas_100g) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                 (n_nombre.strip().title(), n_porcion, n_cal, n_pro, n_car, n_gra, n_cal * factor_100, n_pro * factor_100, n_car * factor_100, n_gra * factor_100))
                    conn.commit()
                    st.success(f"¡{n_nombre.title()} agregado a tu base de datos exitosamente!")
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
                    import re
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
                            conn.execute("""INSERT INTO Biblioteca_Alimentos (nombre, porcion_base_g, cal_100g, prot_100g, carb_100g, grasas_100g, cal_porcion, prot_porcion, carb_porcion, grasas_porcion) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
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
                delete_selected_rows(conn, "ConsumoDiario", ids_a_borrar)
                st.success(f"{len(ids_a_borrar)} consumos eliminados.")
                st.rerun()

def render_cola(conn, u_id, hoy):
    if not u_id:
        st.warning("Seleccioná paciente en el menú lateral porque esta acción impactará en SU cuenta bancaria calórica."); return
        
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
                    import os
                    if os.path.exists(row['contenido']):
                        st.image(row['contenido'], width=400)
                        if st.button("Tratamiento OCR sobre Sangría Diaria", key=f"ocr_{row['id']}"):
                            stat = extract_calories_ocr(row['contenido'])
                            if isinstance(stat, float):
                                st.success(f"Inyectando Actividad ({stat} Kal) a {st.session_state['active_user_name']}.")
                                from database import sumar_calorias_activas
                                sumar_calorias_activas(conn, u_id, hoy, stat)
                                conn.execute("UPDATE Pendientes SET procesado = 1 WHERE id = ?", (int(row['id']),))
                                conn.commit()
                                st.rerun()
                            elif isinstance(stat, dict) and stat:
                                total_kcal = sum(stat.values())
                                st.success(f"Inyectando Actividad Multi-día ({total_kcal} kcal) a {st.session_state['active_user_name']}.")
                                from database import sumar_calorias_activas
                                for d_str, cals in stat.items():
                                    sumar_calorias_activas(conn, u_id, d_str, cals)
                                conn.execute("UPDATE Pendientes SET procesado = 1 WHERE id = ?", (int(row['id']),))
                                conn.commit()
                                st.rerun()
                            else: st.error(str(stat) if not isinstance(stat, dict) else "No se detectaron calorías válidas.")
                    else: st.warning("Archivo Caché Eliminado por el sistema host.")
                        
                if st.button("Depurar Fila de Base Aislada", key=f"del_{row['id']}"):
                    conn.execute("UPDATE Pendientes SET procesado = 1 WHERE id = ?", (int(row['id']),))
                    conn.commit()
                    st.rerun()

if __name__ == "__main__":
    main()
