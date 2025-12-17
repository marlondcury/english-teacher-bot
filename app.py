import os
import uuid
import asyncio
import requests
import psycopg2
from flask import Flask, request, url_for, render_template
from twilio.twiml.messaging_response import MessagingResponse
from groq import Groq
import edge_tts

# --- NOVO: CARREGAR O .ENV (Para funcionar no seu Mac) ---
from dotenv import load_dotenv
load_dotenv() 
# ---------------------------------------------------------

app = Flask(__name__)

# ================= CONFIGURAÇÃO E SEGURANÇA =================
# Pega as chaves do arquivo .env (local) ou das Variáveis do Railway (nuvem)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL") 

# Inicializa o cliente da Groq apenas se a chave existir
if GROQ_API_KEY:
    client = Groq(api_key=GROQ_API_KEY)
else:
    print("⚠️ AVISO: GROQ_API_KEY não encontrada.")
    client = None

# --- NOVO CÉREBRO: O NUTRICIONISTA ---
SYSTEM_PROMPT = """
You are a strict and precise Nutritionist AI specialized in Hypertrophy.
The user is Male, 1.78m, 83.7kg.

YOUR DAILY TARGETS FOR THE USER:
- Calories: 2900 kcal
- Protein: 180g
- Carbs: 350g
- Fats: 80g

INSTRUCTIONS:
1. When the user tells you what they ate, estimate the macros (Calories, Protein, Carbs, Fat).
2. Subtract these values from the Daily Targets.
3. Tell the user EXACTLY what represents in percentage of their day (e.g., "This meal was 20% of your daily protein").
4. Tell them how much is left for the day.
5. Keep answers short and direct.
6. Speak in Portuguese (Brazil).
7. If the food is "dirty" (junk food), scold the user slightly.
"""
# --- FUNÇÕES DO BANCO DE DADOS (POSTGRESQL) ---

def get_db_connection():
    """Conecta ao PostgreSQL"""
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"⚠️ Erro de conexão com banco: {e}")
        return None

def init_db():
    """Cria a tabela se ela não existir"""
    conn = get_db_connection()
    if not conn:
        print("⚠️ Rodando sem banco de dados (Modo Sem Memória)")
        return

    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversas (
                id SERIAL PRIMARY KEY, 
                user_id TEXT,
                role TEXT,
                content TEXT
            )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Banco de Dados conectado!")
    except Exception as e:
        print(f"Erro ao iniciar banco: {e}")

def salvar_mensagem(user_id, role, content):
    conn = get_db_connection()
    if not conn: return # Se não tem banco, só ignora

    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO conversas (user_id, role, content) VALUES (%s, %s, %s)', 
                      (user_id, role, content))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Erro ao salvar: {e}")

def recuperar_historico(user_id):
    conn = get_db_connection()
    # Se não tem banco, retorna só o prompt do sistema
    if not conn: return [{"role": "system", "content": SYSTEM_PROMPT}]

    try:
        cursor = conn.cursor()
        # Pega as ultimas 10 mensagens
        cursor.execute('''
            SELECT role, content 
            FROM conversas 
            WHERE user_id = %s 
            ORDER BY id DESC 
            LIMIT 10
        ''', (user_id,))
        
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        # Inverte para ficar na ordem cronológica (antigo -> novo)
        rows.reverse() 
        
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for row in rows:
            messages.append({"role": row[0], "content": row[1]})
            
        return messages
    except Exception as e:
        print(f"Erro ao ler histórico: {e}")
        return [{"role": "system", "content": SYSTEM_PROMPT}]

def limpar_memoria(user_id):
    conn = get_db_connection()
    if not conn: return

    try:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM conversas WHERE user_id = %s', (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Erro ao limpar: {e}")

# Tenta iniciar o banco ao ligar o app
init_db()

# --- ROTA PRINCIPAL (SITE AMIGÁVEL) ---
@app.route("/")
def home():
    return render_template("index.html")

# --- FUNÇÕES DE ÁUDIO E IA ---
def transcrever_audio(url_audio_whatsapp, content_type):
    if not client: return ""
    nome_arquivo = ""
    try:
        # Detecção iPhone (.m4a) vs Android (.ogg)
        extensao = ".ogg"
        if "mp4" in content_type or "m4a" in content_type: extensao = ".m4a"
        elif "mp3" in content_type: extensao = ".mp3"
            
        nome_arquivo = f"entrada_{uuid.uuid4()}{extensao}"
        
        # Download autenticado do Twilio
        resposta = requests.get(url_audio_whatsapp, auth=(TWILIO_SID, TWILIO_TOKEN))
        
        if resposta.status_code != 200: 
            print("Erro download áudio")
            return ""

        with open(nome_arquivo, 'wb') as f:
            f.write(resposta.content)
            
        with open(nome_arquivo, "rb") as arquivo_audio:
            transcricao = client.audio.transcriptions.create(
                file=(nome_arquivo, arquivo_audio.read()),
                model="whisper-large-v3",
                response_format="json"
            )
        return transcricao.text
    except Exception as e:
        print(f"Erro transcrição: {e}")
        return ""
    finally:
        if nome_arquivo and os.path.exists(nome_arquivo): os.remove(nome_arquivo)

def chat_with_llama(user_id, user_input):
    if not client: return "System Error: AI Key missing."
    
    salvar_mensagem(user_id, "user", user_input)
    historico_completo = recuperar_historico(user_id)
    
    try:
        chat_completion = client.chat.completions.create(
            messages=historico_completo,
            model="llama-3.1-8b-instant",
        )
        resposta = chat_completion.choices[0].message.content
        salvar_mensagem(user_id, "assistant", resposta)
        return resposta
    except Exception as e:
        print(f"Erro Groq: {e}")
        return "I'm having trouble thinking right now."

async def criar_audio_async(texto):
    nome_arquivo = f"resposta_{uuid.uuid4()}.mp3"
    caminho_arquivo = os.path.join("static", nome_arquivo)
    communicate = edge_tts.Communicate(texto, "en-US-AriaNeural")
    await communicate.save(caminho_arquivo)
    return nome_arquivo

# --- WEBHOOK (Onde o WhatsApp bate) ---
@app.route("/bot", methods=['POST'])
def bot():
    user_id = request.values.get('From')
    url_audio = request.values.get('MediaUrl0')
    tipo_audio = request.values.get('MediaContentType0')
    texto_usuario = request.values.get('Body', '').strip()
    
    entrada_final = ""

    if url_audio:
        entrada_final = transcrever_audio(url_audio, tipo_audio)
    elif texto_usuario:
        entrada_final = texto_usuario
    
    resp = MessagingResponse()
    msg = resp.message()

    if entrada_final:
        # Comando secreto para limpar memória
        if entrada_final.lower() == "/reset":
            limpar_memoria(user_id)
            msg.body("Memory cleared!")
            return str(resp)

        resposta_ia = chat_with_llama(user_id, entrada_final)
        msg.body(resposta_ia)
        
        # Gera o áudio da resposta
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            nome_arquivo = loop.run_until_complete(criar_audio_async(resposta_ia))
            loop.close()
            
            # Cria link HTTPS para o WhatsApp tocar
            link_publico = url_for('static', filename=nome_arquivo, _external=True)
            msg.media(link_publico)
        except Exception as e:
            print(f"Erro TTS: {e}")
            
    else:
        msg.body("I couldn't hear you.")

    return str(resp)

if __name__ == "__main__":
    if not os.path.exists('static'): os.makedirs('static')
    app.run(port=5001, debug=True)