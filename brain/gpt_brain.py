import openai
from config import OPENAI_API_KEY

openai.api_key = OPENAI_API_KEY

class GPTBrain:
    def ask(self, question):
        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are Chitti, a futuristic AI assistant."},
                    {"role": "user", "content": question}
                ]
            )
            return response.choices[0].message["content"]
        except:
            return "I am currently offline."
