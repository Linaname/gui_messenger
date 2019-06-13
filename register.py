import tkinter as tk
import asyncio
import json
import re
from chat import readline, writeline
import config


def sanitize_nickname(nickname):
    return re.sub(r'\n', '', nickname)


async def register(nickname, host, port):
        reader, writer = await asyncio.open_connection(host, port)
        greeting = await readline(reader)
        await writeline(writer, '')
        nickname_request_message = await readline(reader)
        await writeline(writer, sanitize_nickname(nickname))
        answer_line_1 = await readline(reader)
        answer_line_2 = await readline(reader)
        json_answer = json.loads(answer_line_1)
        token = json_answer['account_hash']
        return token


async def send_nickname_and_save_token(nickname):
    token = await register(nickname, config.DEFAULT_HOST, config.DEFAULT_SENDER_PORT)
    with open(config.TOKEN_FILE_PATH, 'w') as f:
        f.write(token)


def handle_button_click(input_field):
    nickname = input_field.get()
    asyncio.run(send_nickname_and_save_token(nickname))
    tk.messagebox.showinfo('Готово', 'Никнейм зарегистрирован')
    exit()


def draw():
    root = tk.Tk()
    message = tk.Label(text='Введите новый ник', width=20)
    input_field = tk.Entry(root, width=20)
    button = tk.Button(root, text='Зарегистрировать')
    button.bind('<Button-1>', lambda event: handle_button_click(input_field))
    message.pack()
    input_field.pack()
    button.pack()
    root.mainloop()


if __name__ == '__main__':
    draw()