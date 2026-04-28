# ui_components.py — Componentes UI Reutilizables (CSS, Gráficos Plotly)
import plotly.graph_objects as go
import pandas as pd


# ─── CSS del Dashboard ───────────────────────────────────────
DASHBOARD_CSS = """
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


# ─── Gráfico de Barras Semanal ───────────────────────────────
def create_weekly_chart(df_hist):
    """Crea gráfico de barras agrupadas: Consumo vs Actividad."""
    fig = go.Figure()

    text_cons = [
        f"<b>{val:,.0f}</b><br>Calorías Consumidas" if val > 0 else ""
        for val in df_hist['calorias_consumidas']
    ]
    text_act = [
        f"<b>{val:,.0f}</b><br>Calorías Gastadas" if val > 0 else ""
        for val in df_hist['calorias_activas']
    ]

    fechas_short = pd.to_datetime(df_hist['fecha']).dt.strftime('%d/%m')

    fig.add_trace(go.Bar(
        x=fechas_short, y=df_hist['calorias_consumidas'],
        name='🍽️ CALORIAS CONSUMIDAS',
        marker_color='#B4D330', width=0.38, marker_line_width=0,
        text=text_cons, textposition='outside',
        textfont=dict(size=12, color='#222')
    ))

    fig.add_trace(go.Bar(
        x=fechas_short, y=df_hist['calorias_activas'],
        name='🏃‍♀️ CALORIAS GASTADAS',
        marker_color='#3C8E86', width=0.38, marker_line_width=0,
        text=text_act, textposition='outside',
        textfont=dict(size=12, color='#222')
    ))

    fig.update_layout(
        barmode='group',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(t=50, b=20, l=10, r=10),
        height=380, bargap=0.15,
        xaxis=dict(showgrid=False, zeroline=False, tickfont=dict(size=14, color='#555')),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="center", x=0.5, font=dict(size=13))
    )

    max_y = max(df_hist['calorias_consumidas'].max(), df_hist['calorias_activas'].max()) * 1.2
    fig.update_yaxes(range=[0, max_y])
    return fig


# ─── Gráfico de Barras de Laboratorio ────────────────────────
def create_lab_chart(datos_marc):
    """Crea gráfico de barras para marcadores bioquímicos con coloración de anomalías."""
    fig = go.Figure()

    v_mins = datos_marc['ref_min'].dropna()
    v_maxs = datos_marc['ref_max'].dropna()
    if not v_mins.empty and not v_maxs.empty:
        fig.add_hrect(
            y0=v_mins.mean(), y1=v_maxs.mean(),
            line_width=0, fillcolor="#B4D330", opacity=0.15, layer="below"
        )

    colores = []
    textos = []
    for _, r in datos_marc.iterrows():
        es_anom = False
        if pd.notna(r['ref_min']) and r['valor'] < r['ref_min']:
            es_anom = True
        if pd.notna(r['ref_max']) and r['valor'] > r['ref_max']:
            es_anom = True
        colores.append('#d9534f' if es_anom else '#3C8E86')
        textos.append(f"<b>{r['valor']}</b><br>{r['unidad']}")

    fechas_short = pd.to_datetime(datos_marc['fecha']).dt.strftime('%d/%m/%Y')

    fig.add_trace(go.Bar(
        x=fechas_short, y=datos_marc["valor"],
        marker_color=colores, width=0.35, marker_line_width=0,
        text=textos, textposition='outside',
        textfont=dict(size=12, color='#222')
    ))

    fig.update_layout(
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(t=50, b=20, l=10, r=10),
        height=380,
        xaxis=dict(showgrid=False, zeroline=False, tickfont=dict(size=13, color='#555')),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        showlegend=False
    )

    max_y = datos_marc["valor"].max() * 1.25
    if max_y > 0:
        fig.update_yaxes(range=[0, max_y])

    return fig
