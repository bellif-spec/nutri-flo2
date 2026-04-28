# database.py — Capa de Persistencia (PostgreSQL / Supabase)
import os
import logging
import psycopg2
import psycopg2.extras
import pandas as pd
from sqlalchemy import create_engine
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ─── Conexión ────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    logger.warning("DATABASE_URL no configurada. La aplicación no podrá conectarse a la base de datos.")

_engine = None

def _get_engine():
    global _engine
    if _engine is None and DATABASE_URL:
        _engine = create_engine(DATABASE_URL)
    return _engine


# ─── Wrapper de Compatibilidad SQLite → PostgreSQL ───────────
class DictRow(dict):
    """Emula sqlite3.Row permitiendo acceso por índice numérico y por nombre."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PgCursor:
    """Traduce queries con '?' (estilo SQLite) a '%s' (estilo psycopg2)."""
    def __init__(self, cur):
        self.cur = cur

    def execute(self, query, params=None):
        query = query.replace('?', '%s')
        try:
            if params is not None:
                self.cur.execute(query, params)
            else:
                self.cur.execute(query)
        except Exception as e:
            logger.error(f"Error SQL: {e} | Query: {query[:120]}")
            raise
        return self

    def fetchone(self):
        res = self.cur.fetchone()
        return DictRow(res) if res is not None else None

    def fetchall(self):
        return [DictRow(r) for r in self.cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    """Conexión a PostgreSQL que imita la interfaz de sqlite3."""
    def __init__(self):
        self.conn = psycopg2.connect(DATABASE_URL)
        self.conn.autocommit = False

    def cursor(self):
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        return PgCursor(cur)

    def execute(self, query, params=None):
        cur = self.cursor()
        return cur.execute(query, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            try:
                self.conn.rollback()
            except Exception:
                pass
            logger.error(f"Transacción revertida por error: {exc_val}")
        self.close()
        return False


def get_db_connection():
    """Devuelve una nueva conexión a PostgreSQL."""
    return PgConnection()


# ─── Monkey-patch de pd.read_sql_query ───────────────────────
_original_read_sql = pd.read_sql_query

def _custom_read_sql(sql, con, params=None, **kwargs):
    if isinstance(con, PgConnection):
        sql = sql.replace('?', '%s')
        return _original_read_sql(sql, _get_engine(), params=params, **kwargs)
    return _original_read_sql(sql, con, params=params, **kwargs)

pd.read_sql_query = _custom_read_sql


# ─── Helpers Reutilizables ───────────────────────────────────
def upsert_actividad(conn, id_usuario, fecha):
    """Inserta un registro de actividad diaria si no existe (idempotente)."""
    conn.execute(
        "INSERT INTO ActividadDiaria (id_usuario, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (id_usuario, fecha)
    )

def sumar_calorias_activas(conn, id_usuario, fecha, kcal):
    """Inserta actividad si no existe y suma calorías quemadas."""
    upsert_actividad(conn, id_usuario, fecha)
    conn.execute(
        "UPDATE ActividadDiaria SET calorias_activas = calorias_activas + ? WHERE fecha = ? AND id_usuario = ?",
        (kcal, fecha, id_usuario)
    )

def delete_selected_rows(conn, table, ids):
    """Elimina filas por lista de IDs de forma segura."""
    if not ids:
        return 0
    placeholders = ','.join(['?'] * len(ids))
    conn.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids)
    conn.commit()
    return len(ids)

def get_weekly_data(conn, u_id):
    """Retorna DataFrames de consumo y actividad de los últimos 7 días."""
    query_cons = ("SELECT fecha, SUM(calorias) as calorias_consumidas, "
                  "SUM(proteinas) as proteinas, SUM(carbos) as carbos, SUM(grasas) as grasas "
                  "FROM ConsumoDiario WHERE id_usuario = ? GROUP BY fecha ORDER BY fecha DESC LIMIT 7")
    df_cons = pd.read_sql_query(query_cons, conn, params=(u_id,))

    query_act = ("SELECT fecha, calorias_activas "
                 "FROM ActividadDiaria WHERE id_usuario = ? ORDER BY fecha DESC LIMIT 7")
    df_act = pd.read_sql_query(query_act, conn, params=(u_id,))

    return df_cons, df_act

def calcular_metas(peso, altura, edad, genero, nivel_actividad):
    """Calcula TMB (Mifflin-St Jeor) y metas de recomposición corporal."""
    if genero == "Hombre":
        tmb = (10 * peso) + (6.25 * altura) - (5 * edad) + 5
    else:
        tmb = (10 * peso) + (6.25 * altura) - (5 * edad) - 161

    naf = {"Sedentario": 1.2, "Ligero": 1.375, "Moderado": 1.55, "Intenso": 1.725}
    get = tmb * naf.get(nivel_actividad, 1.2)

    meta_proteina = peso * 2.2
    objetivo_kcal = get * 0.85

    return {
        "tmb": round(tmb),
        "get": round(get),
        "objetivo_kcal": round(objetivo_kcal),
        "meta_proteina": round(meta_proteina)
    }
