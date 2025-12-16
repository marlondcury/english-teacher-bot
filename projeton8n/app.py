import os
import uuid
import asyncio
import requests
import psycopg2 # O banco de dados profissional
from flask import Flask, request, url_for, render_template
from twilio.twiml.messaging_response import MessagingResponse
from groq import Groq
import edge_tts

app = Flask(__name__)

# ================= SEGURANCA E CONFIGURACAO =================
# Agora o codigo busca as chaves nas variaveis de ambiente da nuvem
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL") 

# Se nao tiver chaves (erro comum ao rodar local sem configurar), avisa
if not GROQ_API_KEY:
    print("⚠️ AVISO: Chaves de API nao encontradas. Configure as variaveis de ambiente.")

client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """
You are an English teacher. 
1. Keep your answers SHORT (max 2 sentences).
2. Correct the student's grammar if necessary.
3. Always speak in English.
4. Remember the context of the conversation.
"""

# --- FUNCOES DO BANCO DE DADOS (POSTGRESQL) ---

def get_db_connection():
    """Conecta ao PostgreSQL usando a URL da nuvem"""
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    """Cria a tabela se ela nao existir (Versao Postgres)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # SERIAL = Auto Incremento no Postgres
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
        print("✅ Banco de Dados conectado e verificado!")
    except Exception as e:
        print(f"⚠️ Erro ao conectar no Banco (Isso e normal se rodar local sem configurar): {e}")

def salvar_mensagem(user_id, role, content):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # No Postgres usa-se %s em vez de ?
        cursor.execute('INSERT INTO conversas (user_id, role, content) VALUES (%s, %s, %s)', 
                      (user_id, role, content))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Erro ao salvar mensagem: {e}")

def recuperar_historico(user_id):
    try:
        conn = get_db_connection()
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
        
        rows.reverse() 
        
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for row in rows:
            messages.append({"role": row[0], "content": row[1]})
            
        return messages
    except Exception as e:
        print(f"Erro ao ler banco: {e}")
        return [{"role": "system", "content": SYSTEM_PROMPT}]

def limpar_memoria(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM conversas WHERE user_id = %s', (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Erro ao limpar: {e}")

# Tenta iniciar o banco ao ligar (so funciona se tiver DATABASE_URL)
if DATABASE_URL:
    init_db()

# --- ROTA DA PAGINA INICIAL (AMIGAVEL) ---
@app.route("/")
def home():
    return render_template("index.html")

# --- FUNCOES DE AUDIO E IA ---
def transcrever_audio(url_audio_whatsapp, content_type):
    nome_arquivo = ""
    try:
        extensao = ".ogg"
        if "mp4" in content_type or "m4a" in content_type: extensao = ".m4a"
        elif "mp3" in content_type: extensao = ".mp3"
            
        nome_arquivo = f"entrada_{uuid.uuid4()}{extensao}"
        resposta = requests.get(url_audio_whatsapp, auth=(TWILIO_SID, TWILIO_TOKEN))
        
        if resposta.status_code != 200: return ""

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
        print(f"Erro transcricao: {e}")
        return ""
    finally:
        if nome_arquivo and os.path.exists(nome_arquivo): os.remove(nome_arquivo)

def chat_with_llama(user_id, user_input):
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
    except Exception:
        return "Error calling AI."

async def criar_audio_async(texto):
    nome_arquivo = f"resposta_{uuid.uuid4()}.mp3"
    caminho_arquivo = os.path.join("static", nome_arquivo)
    communicate = edge_tts.Communicate(texto, "en-US-AriaNeural")
    await communicate.save(caminho_arquivo)
    return nome_arquivo

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
        if entrada_final.lower() == "/reset":
            limpar_memoria(user_id)
            msg.body("Memory cleared!")
            return str(resp)

        resposta_ia = chat_with_llama(user_id, entrada_final)
        msg.body(resposta_ia)
        
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            nome_arquivo = loop.run_until_complete(criar_audio_async(resposta_ia))
            loop.close()
            link_publico = url_for('static', filename=nome_arquivo, _external=True)
            msg.media(link_publico)
        except Exception:
            pass
            
    else:
        msg.body("I couldn't hear you.")

    return str(resp)

if __name__ == "__main__":
    if not os.path.exists('static'): os.makedirs('static')
    app.run(port=5001, debug=True)