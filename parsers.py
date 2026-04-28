# parsers.py — Módulos de Parsing: OCR, PDF Clínico, API OpenFoodFacts
import os
import re
import logging
import requests
import pandas as pd
import pdfplumber
import io
from datetime import datetime, timedelta
from PIL import Image

logger = logging.getLogger(__name__)


# ─── API OpenFoodFacts ───────────────────────────────────────
def buscar_alimento_off_api(termino):
    """Busca alimentos en la API pública de OpenFoodFacts."""
    url = f"https://world.openfoodfacts.org/cgi/search.pl?search_terms={termino}&search_simple=1&action=process&json=1"
    headers = {'User-Agent': 'MiNutriApp/6.0 (Cloud)'}
    try:
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        return response.json().get('products', [])
    except requests.exceptions.HTTPError as he:
        logger.warning(f"OpenFoodFacts HTTP error: {he}")
        if response.status_code >= 500:
            return {"error": "🌐 El servidor de OpenFoodFacts se encuentra colapsado momentáneamente (Error 50x).\n\n💡 Alternativa: Buscá tu alimento localmente en la pestaña 'Anotar de Base Local'."}
        return {"error": str(he)}
    except Exception as e:
        logger.error(f"OpenFoodFacts connection failed: {e}")
        return {"error": f"Falla de conexión: {str(e)}"}


# ─── OCR de Calorías (Apple Watch / Fitness) ─────────────────
def extract_calories_ocr(image_path):
    """Extrae calorías activas de capturas de pantalla de Apple Watch/Fitness."""
    try:
        import pytesseract
        import PIL.ImageOps

        # Verificar disponibilidad de Tesseract
        tess_cmd = pytesseract.pytesseract.tesseract_cmd
        if tess_cmd and not os.path.exists(tess_cmd):
            logger.error(f"Tesseract no encontrado en: {tess_cmd}")
            return "Tesseract OCR no enrutado."

        tessdata_path = os.path.join(os.getcwd(), 'tessdata')
        if os.path.exists(tessdata_path):
            os.environ['TESSDATA_PREFIX'] = tessdata_path

        # Preprocesamiento: Invertir colores (Fitness apps usan fondo oscuro)
        img = Image.open(image_path).convert('L')
        img = PIL.ImageOps.invert(img)
        text = pytesseract.image_to_string(img, lang='spa+eng')
        logger.info(f"OCR extrajo {len(text)} caracteres de {image_path}")

        # 1. Intento Multi-Día (Apple Fitness semanal)
        matches = re.findall(
            r'(\d{1,3}(?:[.,]\d{3})*|\d+)\s*[C<]?A[L1]\s*([A-Za-z]+|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
            text, re.IGNORECASE
        )

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
                try:
                    calorias = float(c_clean)
                except ValueError:
                    logger.warning(f"OCR: No se pudo parsear '{cal_str}' como número")
                    continue

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
                            fecha_target = datetime(y, p1, p2)
                            fecha_str = fecha_target.strftime("%Y-%m-%d")
                        except ValueError:
                            try:
                                fecha_target = datetime(y, p2, p1)
                                fecha_str = fecha_target.strftime("%Y-%m-%d")
                            except ValueError:
                                logger.warning(f"OCR: Fecha irreconocible: {dia_str}")

                if fecha_str:
                    resultados[fecha_str] = resultados.get(fecha_str, 0) + calorias

            if resultados:
                logger.info(f"OCR multi-día: {resultados}")
                return resultados

        # 2. Fallback: Extracción diaria clásica
        m = re.search(r'(?i)activas.*?(\d+[\.,]?\d*)', text) or \
            re.search(r'(?i)moverse.*?(\d+[\.,]?\d*)', text)

        if m:
            val = float(m.group(1).replace(',', '.'))
            logger.info(f"OCR single-day: {val} kcal")
            return val

        logger.warning(f"OCR: No se detectaron calorías en {image_path}")
        return "Error Biométrico."

    except Exception as e:
        logger.error(f"OCR falló completamente: {e}")
        return f"Error de lectura: {e}"


# ─── Parser de Laboratorio PDF ───────────────────────────────
def parsear_laboratorio_pdf(archivo_bytes):
    """Extrae marcadores bioquímicos de PDFs de laboratorio (formato CIBIC y genérico)."""
    marcadores_detectados = []
    try:
        with pdfplumber.open(io.BytesIO(archivo_bytes)) as pdf:
            text_completo = ""
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_completo += text + "\n"

            logger.info(f"PDF: {len(pdf.pages)} páginas, {len(text_completo)} caracteres extraídos")

            estado_marcador = None
            for line in text_completo.split('\n'):
                line = line.strip()

                # Regex 1: Identificador numeral CIBIC (Ej: "133 CALCEMIA")
                m_tit = re.match(r'^(\d{2,4})\s+([A-Z0-9\-\s\(\)]+)$', line)
                if m_tit and "COPIA DIGITAL" not in line:
                    estado_marcador = m_tit.group(2).strip()
                    continue

                # Regex 2: "Valor Hallado:" pegado al titular
                m_val = re.search(r'(?i)Valor\shallado[\:\.]*\s*([\d\.\,]+)\s*([a-zA-Z\/\%μµ]+)?', line)
                if m_val and estado_marcador:
                    v = m_val.group(1).replace(',', '.')
                    u = m_val.group(2) if m_val.group(2) else ""
                    try:
                        marcadores_detectados.append({
                            "marcador": estado_marcador,
                            "valor": float(v),
                            "unidad": u.strip(),
                            "ref_min": None,
                            "ref_max": None
                        })
                    except ValueError:
                        logger.warning(f"PDF: Valor no numérico para {estado_marcador}: {v}")
                    estado_marcador = None
                    continue

                # Regex 3: Heurística fallback genérica
                m_gen = re.search(r'^([a-zA-ZáéíóúÁÉÍÓÚñÑ\-\_]{4,35})\s+([\d\.\,]+)\s*([a-zA-Z\/\%μµ]{1,10})?', line)
                if m_gen and not estado_marcador:
                    n = m_gen.group(1).strip()
                    v2 = m_gen.group(2).replace(',', '.')
                    u2 = m_gen.group(3) if m_gen.group(3) else ""
                    exclusiones = ['fecha', 'paciente', 'edad', 'médico', 'página', 'sexo', 'impreso', 'referencia']
                    if not any(f in n.lower() for f in exclusiones):
                        try:
                            if not any(x['marcador'] == n for x in marcadores_detectados):
                                marcadores_detectados.append({
                                    "marcador": n.title(),
                                    "valor": float(v2),
                                    "unidad": u2.strip(),
                                    "ref_min": None,
                                    "ref_max": None
                                })
                        except ValueError:
                            pass

                # Regex 4: Rangos de referencia
                m_ref_rango = re.search(r'(?i)Valor\s+de\s+Referencia[\:\.]*\s*([\d\.\,]+)\s*(?:a|\-)\s*([\d\.\,]+)', line)
                if m_ref_rango and marcadores_detectados:
                    try:
                        marcadores_detectados[-1]['ref_min'] = float(m_ref_rango.group(1).replace(',', '.'))
                        marcadores_detectados[-1]['ref_max'] = float(m_ref_rango.group(2).replace(',', '.'))
                    except ValueError:
                        pass
                    continue

                m_ref_menor = re.search(r'(?i)Valor\s+de\s+Referencia[\:\.]*\s*(?:\<|menor|hasta)\s*([\d\.\,]+)', line)
                if m_ref_menor and marcadores_detectados:
                    try:
                        marcadores_detectados[-1]['ref_min'] = 0.0
                        marcadores_detectados[-1]['ref_max'] = float(m_ref_menor.group(1).replace(',', '.'))
                    except ValueError:
                        pass
                    continue

    except Exception as e:
        logger.error(f"PDF parsing falló: {e}")
        return {"error": str(e)}

    logger.info(f"PDF: {len(marcadores_detectados)} marcadores extraídos")
    return pd.DataFrame(marcadores_detectados)
