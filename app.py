from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import json
import zipfile
import tempfile
import csv
import random
from collections import Counter
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Render stores secret files at /etc/secrets/
CREDENTIALS_PATH = '/etc/secrets2/credentials.json' if os.path.exists('/etc/secrets2/credentials.json') else os.getenv('GOOGLE_CREDENTIALS', 'credentials.json')

TRIGGER_OPCODES = [
    'event_whenflagclicked', 'event_whenbroadcastreceived',
    'event_whenkeypressed', 'control_start_as_clone', 'procedures_definition'
]

current_project = {}
tokens_remaining = 3
conversation_history = []
current_low_dims = []
active_sessions = {}
upload_count = 0


def build_project_summary(targets, scores):
    sprites = [t for t in targets if not t.get('isStage')]
    sprite_names = [t.get('name', 'Unknown') for t in sprites]
    vars_total = sum(len(t.get('variables', {})) for t in targets)
    custom_count = 0

    for t in targets:
        for b in t.get('blocks', {}).values():
            if b.get('opcode') == 'procedures_definition':
                custom_count += 1

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



def check_bad_habits(targets):
    issues = []

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
                msg = b.get('fields', {}).get('BROADCAST_OPTION', ['unknown'])[0]
                keys.append(f'{opcode}:{msg}')
            else:
                keys.append(opcode)

        dupes = {k: c for k, c in Counter(keys).items() if c > 1}
        for key, count in dupes.items():
            label = key.split(':')[1] if ':' in key else key
            issues.append(f'Sprite "{name}" has {count} scripts triggered by "{label}"')

    for t in targets:
        if t.get('isStage'):
            continue
        name = t.get('name', 'Unknown')
        tops = [b for b in t.get('blocks', {}).values() if b.get('topLevel', False)]
        dead = [b for b in tops if b.get('opcode') not in TRIGGER_OPCODES]
        if dead:
            issues.append(f'Sprite "{name}" has {len(dead)} block(s) with no trigger')

    # same default names as Dr. Scratch
    DEFAULT = ['Sprite', 'Objeto', 'Personatge', 'Figura', 'o actor', 'Personaia']
    for t in targets:
        if not t.get('isStage') and t.get('name') in DEFAULT:
            issues.append(f'Default name: "{t["name"]}"')

    return issues


PROJECTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'projects')

def contains_bad_language(text):
    from google import genai as _genai
    _client = _genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
    result = _client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f'Does this message contain hate speech, discriminatory language, or serious insults? Reply only "yes" or "no".\n\nMessage: {text}'
    )
    return result.text.strip().lower().startswith('yes')


def upload_to_drive(file_path, filename):
    from google.cloud import storage
    from google.oauth2.service_account import Credentials as SACredentials
    bucket_name = os.getenv('GCS_BUCKET', 'ct-buddy-sessions')
    creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
    client = storage.Client(credentials=creds, project='ct-buddy-502315')
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(filename)
    blob.upload_from_filename(file_path)
    print(f"Cloud Storage upload success: {filename}")

@app.route('/')
def home():
    return send_file('index.html')

@app.route('/assets/<path:filename>')
def assets(filename):
    return send_file(os.path.join('assets', filename))


@app.route('/get-project', methods=['GET'])
def get_project():
    try:
        files = [f for f in os.listdir(PROJECTS_DIR) if f.endswith('.sb3')]
        if not files:
            return jsonify({'error': 'No projects found'}), 404
        chosen = random.choice(files)
        return send_file(
            os.path.join(PROJECTS_DIR, chosen),
            as_attachment=True,
            download_name=chosen
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/upload', methods=['POST'])
def upload():
    global current_project, tokens_remaining, conversation_history, current_low_dims, upload_count
    try:
        file = request.files.get('file')
        if not file:
            return jsonify({'error': 'No file'}), 400

        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, file.filename)
        file.save(file_path)

        try:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                project_json_content = zip_ref.read('project.json').decode('utf-8')
        except Exception:
            with open(file_path, 'r') as f:
                project_json_content = f.read()

        current_project = json.loads(project_json_content)
        tokens_remaining = 3
        current_low_dims = []

        is_reupload = request.args.get('reupload') == 'true'
        if not is_reupload:
            conversation_history = []
            upload_count = 0
        upload_count += 1

        #save to google drive
        session_id = request.args.get('session_id')
        if session_id:
            try:
                timestamp = datetime.now().strftime('%H%M%S')
                upload_to_drive(file_path, f"{session_id}_{upload_count}_{timestamp}.sb3")
            except Exception as e:
                print(f"Drive upload failed: {e}")

        return jsonify({'status': 'Uploaded', 'upload_count': upload_count}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/analyze', methods=['GET'])
def analyze():
    global current_project, current_low_dims
    try:
        if not current_project:
            return jsonify({'error': 'No project loaded'}), 400

        targets = current_project.get('targets', [])
        issues = check_bad_habits(targets)
        scores = get_scores(current_project)

        LLM_DIMS = {'Logic', 'Abstraction', 'Data Representation', 'Math Operators'}

        ct_scores = {d: v for d, v in scores.items() if d in LLM_DIMS}
        min_score = min(ct_scores.values()) if ct_scores else 4
        low_dims = [d for d, v in ct_scores.items() if v == min_score and v < 4]
        current_low_dims = low_dims

        unlock_llm = len(issues) == 0 and len(low_dims) > 0

        return jsonify({
            'bad_habits_found': len(issues) > 0,
            'bad_habits_issues': issues,
            'dr_scratch_scores': scores,
            'unlock_llm': unlock_llm,
            'low_dims': low_dims
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/start', methods=['GET'])
def start():
    try:
        if not current_project:
            return jsonify({'error': 'No project loaded'}), 400
        targets = current_project.get('targets', [])
        scores = get_scores(current_project)
        low_dims = current_low_dims or ['Logic']
        dims_text = ', '.join(f'{d} ({scores.get(d, 0)}/4)' for d in low_dims)

        if conversation_history:
            conv_text = "\n".join([f"{m['role']}: {m['message']}" for m in conversation_history])
            summary = (
                f"{build_project_summary(targets, scores)}\n"
                f"Low-scoring dimensions: {dims_text}.\n"
                f"Previous conversation:\n{conv_text}\n\n"
                f"The student just re-uploaded a revised version of their project. "
                f"DO NOT greet them as if it is the first time, you already know them and have been talking. "
                f"DO NOT say 'Hey there' or introduce yourself again. Just the first time after they re-upload to greet them again "
                f"Briefly acknowledge the re-upload and what they discussed before, then ask one Socratic follow-up question to continue guiding them."
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

        language = request.args.get('language', 'en')
        from socratic_turn1 import run as ask
        response = ask(summary, low_dims, scores, language=language)
        conversation_history.append({'role': 'ai', 'message': response})
        return jsonify({'ai_response': response, 'tokens_remaining': tokens_remaining}), 200
    except Exception as e:
        print(f"ERROR in start: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/chat', methods=['POST'])
def chat():
    global current_project, tokens_remaining, conversation_history, current_low_dims
    try:
        if not current_project:
            return jsonify({'error': 'No project loaded'}), 400

        data = request.get_json()
        user_question = data.get('question', '')
        language = data.get('language', 'en')

        if not user_question:
            return jsonify({'error': 'No question provided'}), 400

        if contains_bad_language(user_question):
            conversation_history.clear()
            current_project.clear()
            return jsonify({
                'ai_response': 'This session has been ended due to inappropriate language. Your data will not be saved.',
                'session_terminated': True,
                'tokens_remaining': 0,
                'chatbot_locked': True
            }), 200

        targets = current_project.get('targets', [])
        sprite_names = [t.get('name') for t in targets if not t.get('isStage')]
        scores = get_scores(current_project)
        low_dims = current_low_dims or ['Logic']

        dims_text = ', '.join(f'{d} ({scores.get(d, 0)}/4)' for d in low_dims)

        if user_question == '__wrap_up__' or tokens_remaining <= 0:
            try:
                from socratic_turn1 import run as socratic_response

                conv_text = "\n".join([f"{m['role']}: {m['message']}" for m in conversation_history])
                summary = (
                    f"Conversation:\n{conv_text}\n\n"
                    f"Student sprites: {sprite_names}\n"
                    f"Low-scoring dimensions: {dims_text}. "
                    f"Wrap up the conversation. Tell them specifically what blocks to add to fix each low dimension."
                )

                final_msg = socratic_response(summary, low_dims, scores, final=True, language=language)
                return jsonify({
                    'ai_response': final_msg,
                    'tokens_remaining': 0,
                    'chatbot_locked': True,
                    'is_final_summary': True
                }), 200
            except Exception as e:
                print(f"ERROR in final summary: {e}")
                return jsonify({
                    'ai_response': "You've used all 3 attempts. Revise your code based on what we discussed and upload again.",
                    'tokens_remaining': 0,
                    'chatbot_locked': True
                }), 200

        conversation_history.append({'role': 'user', 'message': user_question})
        proj_summary = build_project_summary(targets, scores)

        is_last = tokens_remaining == 1 and len(conversation_history) > 1

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
            print(f"ERROR calling Gemini: {e}")
            ai_response = "Oops, something went wrong on our end. Please try sending your message again."

        conversation_history.append({'role': 'ai', 'message': ai_response})
        tokens_remaining -= 1

        return jsonify({
            'ai_response': ai_response,
            'tokens_remaining': tokens_remaining,
            'chatbot_locked': tokens_remaining <= 0
        }), 200
    except Exception as e:
        print(f"ERROR in chat: {e}")
        return jsonify({'error': str(e)}), 500


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

# GOOGLE CREDENTIALS AND SHEET 


def append_to_sheets(row_data):
    from google.cloud import bigquery
    from google.oauth2.service_account import Credentials as SACredentials
    creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
    client = bigquery.Client(credentials=creds, project='ct-buddy-502315', location='us-central1')
    table_id = 'ct-buddy-502315.ct_buddy.sessions'
    row = dict(zip(CSV_HEADERS, row_data))
    try:
        row['attempt'] = int(row['attempt']) if row.get('attempt') not in ('', None) else None
    except (ValueError, TypeError):
        row['attempt'] = None
    errors = client.insert_rows_json(table_id, [row])
    if errors:
        raise Exception(f"BigQuery insert errors: {errors}")
    print(f"BigQuery insert success")


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


############# GOOGLE CREDENTIALS AND SHEET 


def gcs_save_session(filename, data):
    from google.cloud import storage
    from google.oauth2.service_account import Credentials as SACredentials
    creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
    client = storage.Client(credentials=creds, project='ct-buddy-502315')
    bucket = client.bucket(os.getenv('GCS_BUCKET', 'ct-buddy-sessions'))
    blob = bucket.blob(f"sessions/{filename}.json")
    blob.upload_from_string(json.dumps(data), content_type='application/json')

def gcs_load_session(filename):
    from google.cloud import storage
    from google.oauth2.service_account import Credentials as SACredentials
    try:
        creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
        client = storage.Client(credentials=creds, project='ct-buddy-502315')
        bucket = client.bucket(os.getenv('GCS_BUCKET', 'ct-buddy-sessions'))
        blob = bucket.blob(f"sessions/{filename}.json")
        return json.loads(blob.download_as_text())
    except Exception:
        return {}

def gcs_delete_session(filename):
    from google.cloud import storage
    from google.oauth2.service_account import Credentials as SACredentials
    try:
        creds = SACredentials.from_service_account_file(CREDENTIALS_PATH)
        client = storage.Client(credentials=creds, project='ct-buddy-502315')
        bucket = client.bucket(os.getenv('GCS_BUCKET', 'ct-buddy-sessions'))
        bucket.blob(f"sessions/{filename}.json").delete()
    except Exception:
        pass

@app.route('/save', methods=['POST'])
def save():
    try:
        data = request.get_json()
        filename = data.get('filename')

        if filename:
            session = gcs_load_session(filename)
            session['after_scores'] = data.get('after_scores')
            row = build_row(session)
            try:
                append_to_sheets(row)
                print(f"Sheets update success: {filename}")
            except Exception as e:
                print(f"Sheets update failed: {e}")
            gcs_delete_session(filename)
        else:
            filename = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            gcs_save_session(filename, data)
            row = build_row(data)
            try:
                append_to_sheets(row)
                print(f"Sheets save success: {filename}")
            except Exception as e:
                print(f"Sheets save failed: {e}")

        return jsonify({'status': 'saved', 'filename': filename}), 200
    except Exception as e:
        print(f"Save route error: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5002)
