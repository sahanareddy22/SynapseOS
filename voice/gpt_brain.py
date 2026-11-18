from openai import OpenAI

client = OpenAI(api_key="YOUR_API_KEY_HERE")

class GPTBrain:

    def __init__(self):
        self.history = []

    def ask(self, message):
        self.history.append({"role": "user", "content": message})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=self.history
        )

        reply = response.choices[0].message.content
        self.history.append({"role": "assistant", "content": reply})

        return reply
