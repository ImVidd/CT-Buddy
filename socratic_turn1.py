import sys
import json
import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
API_KEY = os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=API_KEY)

# Guiding angle for each low-scoring dimension — keeps questions on topic
DIM_HINTS = {
    'Logic': "decisions and conditions — if/else blocks, checking when something is true or false",
    'Abstraction': "repeated actions across sprites that could be one custom block instead of copied code",
    'Data Representation': "information the game needs to remember — variables, lists, keeping score or tracking state",
    'Math Operators': "numbers and calculations — adding, comparing, using formulas in the project",
}

def build_prompt(summary, low_dims, scores, final=False, language='en'):
    score_text = '\n'.join([f'  {k}: {v}/4' for k, v in scores.items()])
    dims_text = ', '.join(low_dims)
    hints = '\n'.join([f'  - {d}: think about {DIM_HINTS.get(d, d)}' for d in low_dims])

    lang_instruction = "Respond in Spanish." if language == 'es' else "Respond in English."

    if final:
        return f"""You are CT-Buddy, a Socratic tutor wrapping up a conversation with a student about their Scratch project.

Write a warm, specific closing message (3-5 sentences) that:
1. Acknowledges something the student said during the conversation
2. Names the low-scoring dimension(s): {dims_text}
3. Tells them concretely which Scratch blocks or concepts to try next to improve each dimension
4. Ends with encouragement to revise and re-upload

Project info:
{summary}

Dr. Scratch scores:
{score_text}

{lang_instruction}
"""

    return f"""You are CT-Buddy, a Socratic tutor helping a school student improve their Scratch project.
The student scored low on: {dims_text}

Your goal is to guide them to discover the concept themselves through conversation — never give away the answer directly.

How to respond:
- Read the conversation history carefully and NEVER repeat a question or idea you already asked
- Each response should move the conversation forward — build on what the student just said
- Ask ONE short guiding question that helps them think deeper about the low-scoring dimension
- If the student has no idea (vague, "I don't know", off-topic), give one small concrete hint from their own project, then ask again
- If the student is on the right track, push them one step further
- Never name the specific Scratch block that solves the problem
- Be casual and encouraging — like a patient older student helping a friend, not a teacher lecturing
- Keep it short: one question, or one hint + one question max

Dimension guidance (use this to stay on topic):
{hints}

Project info:
{summary}

Dr. Scratch scores:
{score_text}

{lang_instruction}
"""

def run(summary, low_dims, scores, final=False, language='en'):
    prompt = build_prompt(summary, low_dims, scores, final=final, language=language)

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
    )

    print('\nCT-Buddy says:')
    print(response.text)
    return response.text

if __name__ == '__main__':
    if len(sys.argv) > 1:
        data = json.loads(sys.argv[1])
        run(data['summary'], data['low_dims'], data['scores'])
    else:
        print("Please provide project data.")
