from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import os, json, zipfile, tempfile, random, re
from collections import Counter
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB

CREDENTIALS_PATH = ('/etc/secrets2/credentials.json'
                    if os.path.exists('/etc/secrets2/credentials.json')
                    else os.getenv('GOOGLE_CREDENTIALS', 'credentials.json'))
GCS_BUCKET = os.getenv('GCS_BUCKET', 'ct-buddy-sessions')
GCP_PROJECT = 'ct-buddy-502315'
BQ_TABLE = f'{GCP_PROJECT}.ct_buddy.sessions'

TRIGGER_OPCODES = [
    'event_whenflagclicked', 'event_whenbroadcastreceived',
    'event_whenkeypressed', 'control_start_as_clone', 'procedures_definition'
]

PROJECTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'projects')

DIMS = ['Logic', 'Abstraction', 'Data Representation', 'Math Operators',
        'Parallelism', 'Synchronization', 'Flow Control', 'User Interactivity', 'Motion Operators']
DIMS_BQ = ['Logic', 'Abstraction', 'Data_Representation', 'Math_Operators',
           'Parallelism', 'Synchronization', 'Flow_Control', 'User_Interactivity', 'Motion_Operators']
CSV_HEADERS = (
    ['session_id', 'timestamp', 'attempt'] +
    [f'before_{d}' for d in DIMS_BQ] +
    [f'after_{d}' for d in DIMS_BQ] +
    ['conversation', 'ratings']
)

DEFAULT_SPRITE_RE = re.compile(r'^(Sprite|Objeto|Personaje|Personatge|Figura|Ator)\d*$', re.IGNORECASE)


# ── GCS session helpers ────────────────────────────────────────────────────────

def _gcs_bucket():
    from google.cloud import storage
    from google.oauth2.service_account import Credentials as SACredentials
    creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
    return storage.Client(credentials=creds, project=GCP_PROJECT).bucket(GCS_BUCKET)

def load_session(session_id):
    try:
        blob = _gcs_bucket().blob(f"sessions/{session_id}.json")
        return json.loads(blob.download_as_text())
    except Exception:
        return {}

def save_session(session_id, data):
    blob = _gcs_bucket().blob(f"sessions/{session_id}.json")
    blob.upload_from_string(json.dumps(data), content_type='application/json')


# ── Project helpers ────────────────────────────────────────────────────────────

def build_project_summary(targets, scores):
    sprites = [t for t in targets if not t.get('isStage')]
    sprite_names = [t.get('name', 'Unknown') for t in sprites]
    vars_total = sum(len(t.get('variables', {})) for t in targets)
    custom_count = sum(
        1 for t in targets
        for b in t.get('blocks', {}).values()
        if b.get('opcode') == 'procedures_definition'
    )
    summary = f"This Scratch project has {len(sprites)} sprites: {', '.join(sprite_names)}. "
    summary += f"It uses {vars_total} variables and {custom_count} custom blocks."
    return summary


def get_scores(json_project):
    from hairball3.mastery import Mastery
    mastery = Mastery(filename='project', json_project=json_project)
    raw = mastery.get_scores()
    return {
        'Logic':               raw.get('Logic', 0),
        'Abstraction':         raw.get('Abstraction', 0),
        'Data Representation': raw.get('DataRepresentation', 0),
        'Math Operators':      raw.get('MathOperators', 0),
        'Flow Control':        raw.get('FlowControl', 0),
        'Synchronization':     raw.get('Synchronization', 0),
        'Parallelism':         raw.get('Parallelization', 0),
        'User Interactivity':  raw.get('UserInteractivity', 0),
        'Motion Operators':    raw.get('MotionOperators', 0),
    }


def check_bad_habits(targets, language='en'):
    issues = []
    es = language == 'es'

    for t in targets:
        if t.get('isStage'):
            continue
        name = t.get('name', 'Unknown')
        blocks = t.get('blocks', {})
        tops = [b for b in blocks.values() if b.get('topLevel', False)]

        keys = []
        for b in tops:
            opcode = b.get('opcode', '')
            if opcode == 'event_whenbroadcastreceived':
                try:
                    msg = b.get('fields', {}).get('BROADCAST_OPTION', ['unknown'])[0]
                except (IndexError, TypeError):
                    msg = 'unknown'
                keys.append(f'{opcode}:{msg}')
            else:
                keys.append(opcode)

        for key, count in Counter(keys).items():
            if count > 1:
                label = key.split(':')[1] if ':' in key else key
                issues.append(
                    f'El sprite "{name}" tiene {count} scripts activados por "{label}"' if es
                    else f'Sprite "{name}" has {count} scripts triggered by "{label}"'
                )

        dead = [b for b in tops if b.get('opcode') not in TRIGGER_OPCODES]
        if dead:
            issues.append(
                f'El sprite "{name}" tiene {len(dead)} bloque(s) sin activador' if es
                else f'Sprite "{name}" has {len(dead)} block(s) with no trigger'
            )

        if DEFAULT_SPRITE_RE.match(name):
            issues.append(
                f'Nombre predeterminado: "{name}"' if es
                else f'Default name: "{name}"'
            )

    return issues


def contains_bad_language(text):
    try:
        from google import genai as _genai
        _client = _genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
        result = _client.models.generate_content(
            model='gemini-3.5-flash',
            contents=f'Does this message contain hate speech, discriminatory language, or serious insults? Reply only "yes" or "no".\n\nMessage: """{text}"""'
        )
        return result.text.strip().lower().startswith('yes')
    except Exception as e:
        print(f"Moderation check failed (fail open): {e}")
        return False


def upload_to_gcs(file_path, blob_name):
    from google.cloud import storage
    from google.oauth2.service_account import Credentials as SACredentials
    creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
    client = storage.Client(credentials=creds, project=GCP_PROJECT)
    client.bucket(GCS_BUCKET).blob(blob_name).upload_from_filename(file_path)
    print(f"Cloud Storage upload success: {blob_name}")


# ── BigQuery ──────────────────────────────────────────────────────────────────

def append_to_sheets(row_data):
    from google.cloud import bigquery
    from google.oauth2.service_account import Credentials as SACredentials
    creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
    client = bigquery.Client(credentials=creds, project=GCP_PROJECT, location='us-central1')
    row = dict(zip(CSV_HEADERS, row_data))
    try:
        row['attempt'] = int(row['attempt']) if row.get('attempt') not in ('', None) else None
    except (ValueError, TypeError):
        row['attempt'] = None
    errors = client.insert_rows_json(BQ_TABLE, [row])
    if errors:
        raise Exception(f"BigQuery insert errors: {errors}")
    print("BigQuery insert success")


def build_row(data):
    before = data.get('initial_scores') or {}
    after = data.get('after_scores') or {}
    messages = data.get('messages') or []
    ratings = data.get('ratings') or {}
    conv = ' | '.join([f"{m['role']}: {m.get('text', m.get('message', ''))}" for m in messages])
    ratings_str = ', '.join([f"msg{k}:{v}" for k, v in ratings.items() if v])

    def score(d_map, dim):
        v = d_map.get(dim)
        return float(v) if v is not None and v != '' else None

    return (
        [data.get('session_id', ''), data.get('timestamp', ''), data.get('attempt', '')] +
        [score(before, d) for d in DIMS] +
        [score(after, d) for d in DIMS] +
        [conv, ratings_str]
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return send_file('index.html')


@app.route('/assets/<path:filename>')
def assets(filename):
    return send_from_directory('assets', filename)


@app.route('/get-project', methods=['GET'])
def get_project():
    try:
        files = [f for f in os.listdir(PROJECTS_DIR) if f.endswith('.sb3')]
        if not files:
            return jsonify({'error': 'No projects found'}), 404
        chosen = random.choice(files)
        return send_file(os.path.join(PROJECTS_DIR, chosen), as_attachment=True, download_name=chosen)
    except Exception:
        return jsonify({'error': 'Could not load project'}), 500


@app.route('/upload', methods=['POST'])
def upload():
    try:
        session_id = request.args.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'No session_id'}), 400

        file = request.files.get('file')
        if not file:
            return jsonify({'error': 'No file'}), 400

        filename = secure_filename(file.filename or 'project.sb3')
        if not filename.endswith(('.sb3', '.json')):
            return jsonify({'error': 'Invalid file type'}), 400

        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, filename)
        file.save(file_path)

        try:
            with zipfile.ZipFile(file_path, 'r') as z:
                info = z.getinfo('project.json')
                if info.file_size > 20 * 1024 * 1024:
                    return jsonify({'error': 'Project too large'}), 400
                project_json_content = z.read('project.json').decode('utf-8')
        except Exception:
            with open(file_path, 'r') as f:
                project_json_content = f.read()

        project = json.loads(project_json_content)

        session = load_session(session_id)
        is_reupload = request.args.get('reupload') == 'true'

        if not is_reupload:
            session['conversation_history'] = []
            session['upload_count'] = 0

        session['project'] = project
        session['tokens_remaining'] = 3
        session['low_dims'] = []
        session['upload_count'] = session.get('upload_count', 0) + 1

        save_session(session_id, session)

        try:
            ts = datetime.now().strftime('%H%M%S')
            upload_to_gcs(file_path, f"{session_id}_{session['upload_count']}_{ts}.sb3")
        except Exception as e:
            print(f"GCS archive upload failed: {e}")

        return jsonify({'status': 'Uploaded', 'upload_count': session['upload_count']}), 200
    except Exception as e:
        print(f"Upload error: {e}")
        return jsonify({'error': 'Upload failed'}), 500


@app.route('/analyze', methods=['GET'])
def analyze():
    try:
        session_id = request.args.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'No session_id'}), 400

        session = load_session(session_id)
        project = session.get('project')
        if not project:
            return jsonify({'error': 'No project loaded'}), 400

        language = request.args.get('language', 'en')
        targets = project.get('targets', [])
        issues = check_bad_habits(targets, language=language)
        scores = get_scores(project)

        LLM_DIMS = {'Logic', 'Abstraction', 'Data Representation', 'Math Operators'}
        ct_scores = {d: v for d, v in scores.items() if d in LLM_DIMS}
        min_score = min(ct_scores.values()) if ct_scores else 4
        low_dims = [d for d, v in ct_scores.items() if v == min_score and v < 4]

        session['low_dims'] = low_dims
        save_session(session_id, session)

        return jsonify({
            'bad_habits_found': len(issues) > 0,
            'bad_habits_issues': issues,
            'dr_scratch_scores': scores,
            'unlock_llm': len(issues) == 0 and len(low_dims) > 0,
            'low_dims': low_dims
        }), 200
    except Exception as e:
        print(f"Analyze error: {e}")
        return jsonify({'error': 'Analysis failed'}), 500


@app.route('/start', methods=['GET'])
def start():
    try:
        session_id = request.args.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'No session_id'}), 400

        session = load_session(session_id)
        project = session.get('project')
        if not project:
            return jsonify({'error': 'No project loaded'}), 400

        targets = project.get('targets', [])
        scores = get_scores(project)
        low_dims = session.get('low_dims') or ['Logic']
        conversation_history = session.get('conversation_history', [])
        dims_text = ', '.join(f'{d} ({scores.get(d, 0)}/4)' for d in low_dims)
        language = request.args.get('language', 'en')

        if conversation_history:
            conv_text = "\n".join([f"{m['role']}: {m['message']}" for m in conversation_history])
            summary = (
                f"{build_project_summary(targets, scores)}\n"
                f"Low-scoring dimensions: {dims_text}.\n"
                f"Previous conversation:\n{conv_text}\n\n"
                f"The student just re-uploaded a revised version of their project. "
                f"DO NOT greet them as if it is the first time, you already know them and have been talking. "
                f"DO NOT say 'Hey there' or introduce yourself again. "
                f"Briefly acknowledge the re-upload and what they discussed before, then ask one Socratic follow-up question."
            )
        else:
            summary = (
                f"{build_project_summary(targets, scores)}\n"
                f"Low-scoring dimensions: {dims_text}.\n"
                f"The student just uploaded their project for the first time. "
                f"Give a friendly 2-sentence summary of what their project does, then list exactly 3 specific things they could improve, "
                f"each tied to one of the low-scoring dimensions. Number them 1, 2, 3. "
                f"End with one sentence inviting them to pick one to work on or ask a question. Do NOT ask a Socratic question yet."
            )

        from socratic_turn1 import run as ask
        response = ask(summary, low_dims, scores, language=language)

        conversation_history.append({'role': 'ai', 'message': response})
        session['conversation_history'] = conversation_history
        save_session(session_id, session)

        return jsonify({'ai_response': response, 'tokens_remaining': session.get('tokens_remaining', 3)}), 200
    except Exception as e:
        print(f"Start error: {e}")
        return jsonify({'error': 'Could not start session'}), 500


@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        session_id = data.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'No session_id'}), 400

        session = load_session(session_id)
        project = session.get('project')
        if not project:
            return jsonify({'error': 'No project loaded'}), 400

        user_question = data.get('question', '')
        language = data.get('language', 'en')
        if not user_question:
            return jsonify({'error': 'No question'}), 400

        conversation_history = session.get('conversation_history', [])
        tokens_remaining = session.get('tokens_remaining', 3)
        low_dims = session.get('low_dims') or ['Logic']

        if contains_bad_language(user_question):
            return jsonify({
                'ai_response': ('Esta sesión ha sido terminada debido a lenguaje inapropiado.' if language == 'es'
                                else 'This session has been ended due to inappropriate language.'),
                'session_terminated': True,
                'tokens_remaining': 0,
                'chatbot_locked': True
            }), 200

        targets = project.get('targets', [])
        scores = get_scores(project)
        dims_text = ', '.join(f'{d} ({scores.get(d, 0)}/4)' for d in low_dims)

        conversation_history.append({'role': 'user', 'message': user_question})
        proj_summary = build_project_summary(targets, scores)
        is_last = tokens_remaining == 1

        if is_last:
            conv_text = "\n".join([f"{m['role']}: {m['message']}" for m in conversation_history])
            summary = (
                f"Conversation:\n{conv_text}\n\n"
                f"{proj_summary}\n"
                f"Low-scoring dimensions: {dims_text}.\n"
                f"The student's last message was: \"{user_question}\". "
                f"Acknowledge their idea or answer directly in 1-2 sentences, then wrap up by telling them "
                f"specifically what Scratch blocks to try to improve each low dimension."
            )
        else:
            conv_text = "\n".join([f"{m['role']}: {m['message']}" for m in conversation_history])
            summary = (
                f"Conversation so far:\n{conv_text}\n\n"
                f"{proj_summary}\n"
                f"Low-scoring dimensions: {dims_text}.\n"
                f"The student just said: \"{user_question}\". "
                f"DO NOT introduce yourself or say 'Hey I'm CT-Buddy' — the conversation is already in progress. "
                f"Respond directly by building on what was just said."
            )

        try:
            from socratic_turn1 import run as socratic_response
            ai_response = socratic_response(summary, low_dims, scores, final=is_last, language=language)
        except Exception as e:
            print(f"Gemini error: {e}")
            ai_response = ('Lo siento, algo salió mal. Por favor intenta de nuevo.' if language == 'es'
                           else 'Oops, something went wrong. Please try sending your message again.')

        conversation_history.append({'role': 'ai', 'message': ai_response})
        tokens_remaining -= 1
        session['conversation_history'] = conversation_history
        session['tokens_remaining'] = tokens_remaining
        save_session(session_id, session)

        return jsonify({
            'ai_response': ai_response,
            'tokens_remaining': tokens_remaining,
            'chatbot_locked': tokens_remaining <= 0
        }), 200
    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({'error': 'Chat failed'}), 500


@app.route('/save', methods=['POST'])
def save():
    try:
        data = request.get_json()
        session_id = data.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'No session_id'}), 400

        session = load_session(session_id)

        for field in ('initial_scores', 'messages', 'ratings', 'after_scores', 'attempt', 'timestamp'):
            if data.get(field) is not None:
                session[field] = data[field]

        save_session(session_id, session)

        row = build_row(session)
        try:
            append_to_sheets(row)
            print(f"BigQuery insert success: {session_id}")
        except Exception as e:
            print(f"BigQuery insert failed: {e}")

        return jsonify({'status': 'saved'}), 200
    except Exception as e:
        print(f"Save error: {e}")
        return jsonify({'error': 'Save failed'}), 500


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5002)
