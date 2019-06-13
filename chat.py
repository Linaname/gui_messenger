import config
import asyncio
import async_timeout
import aionursery
from datetime import datetime
import aiofile
import gui
import json
import re
import logging
import concurrent
import socket
from tkinter import messagebox
import contextlib


class InvalidTokenException(Exception):
    pass


@contextlib.asynccontextmanager
async def create_handy_nursery():
    try:
        async with aionursery.Nursery() as nursery:
            yield nursery
    except aionursery.MultiError as e:
        if len(e.exceptions) == 1:
            raise e.exceptions[0]
        raise


async def readline(reader):
    received_data = await reader.readline()
    recieved_string = received_data.decode()[:-1]
    return recieved_string


def sanitize_message(message):
    return re.sub(r'(^\n+)|(\n+(?=\n))|(\n+$)', '', message)


async def writeline(writer, string):
    prepared_string = sanitize_message(string)
    writer.write(prepared_string.encode() + b'\n')
    await writer.drain()


def validate_token(token):
    if '\n' in token:
        raise InvalidTokenException()


async def retry_connection(host, port,
                           status_updates_queue=None,
                           connection_change_class=None,
                           delay=config.RETRY_CONNECTION_DELAY,
                           tries_without_delay=config.RETRIES_WITHOUT_DELAY):
    failed_tries_count = 0
    update_status = (status_updates_queue is not None and
                     connection_change_class is not None)
    while True:
        try:
            if update_status:
                status_updates_queue.put_nowait(connection_change_class.INITIATED)
            reader, writer = await asyncio.open_connection(host, port)
            break
        except (ConnectionRefusedError, socket.gaierror):
            failed_tries_count += 1
            if update_status:
                status_updates_queue.put_nowait(connection_change_class.CLOSED)
            if failed_tries_count >= tries_without_delay:
                await asyncio.sleep(delay)
    if update_status:
        status_updates_queue.put_nowait(connection_change_class.ESTABLISHED)
    return reader, writer


async def async_messages_generator(host, port, status_updates_queue):
    while True:

        reader, writer = await retry_connection(host, port,
                                                status_updates_queue,
                                                gui.ReadConnectionStateChanged)
        while True:
            message_data = await reader.readline()
            message_text = message_data.decode()[:-1]
            datetime_format = '%d.%m.%y %H:%M'
            datetime_string = datetime.now().strftime(datetime_format)
            formatted_message = f'[{datetime_string}] {message_text}'
            yield formatted_message


async def save_messages(filepath, queue):
    async with aiofile.AIOFile(filepath, 'a') as history_file:
        writer = aiofile.Writer(history_file)
        while True:
            message = await queue.get()
            await writer(f'{message}\n')


async def generate_msgs(host, port, gui_queue, file_queue, status_updates_queue, watchdog_queue):
    messages_generator = async_messages_generator(host, port, status_updates_queue)
    async for message in messages_generator:
        watchdog_queue.put_nowait('New message in chat')
        gui_queue.put_nowait(message)
        file_queue.put_nowait(message)


async def authorize(host, port, token, status_updates_queue, watchdog_queue):
    validate_token(token)
    reader, writer = await retry_connection(host, port, status_updates_queue,
                                            gui.SendingConnectionStateChanged)
    greeting = await readline(reader)
    watchdog_queue.put_nowait('Prompt before auth')
    await writeline(writer, token)
    answer_line_1 = await readline(reader)
    answer_line_2 = await readline(reader)
    json_answer = json.loads(answer_line_1)
    if json_answer is None:
        raise InvalidTokenException()
    watchdog_queue.put_nowait('Authorization done')
    return reader, writer, json_answer


async def send_message(reader, writer, message, timeout=config.TIMEOUT):
    await writeline(writer, message)
    if message:
        await writeline(writer, '')
    await writer.drain()
    answer = await asyncio.wait_for(readline(reader), timeout)
    return answer


async def send_msgs(reader, writer,  sending_queue, watchdog_queue, lock):
    while True:
        message = await sending_queue.get()
        async with lock:
            answer = await send_message(reader, writer, message)
        watchdog_queue.put_nowait('Message sent')


async def ping(reader, writer, interval, watchdog_queue, lock):
    while True:
        await asyncio.sleep(interval)
        async with lock:
            answer = await send_message(reader, writer, '')
        watchdog_queue.put_nowait('Ping message')


async def authorize_and_send_msgs(host, port, token, sending_queue, status_updates_queue, watchdog_queue, ping_interval):
    reader, writer, json_answer = await authorize(host, port, token, status_updates_queue, watchdog_queue)
    nickname_update = gui.NicknameReceived(json_answer['nickname'])
    status_updates_queue.put_nowait(nickname_update)
    sender_lock = asyncio.Lock()
    async with create_handy_nursery() as nursery:
        nursery.start_soon(send_msgs(reader, writer, sending_queue, watchdog_queue, sender_lock))
        nursery.start_soon(ping(reader, writer, ping_interval, watchdog_queue, sender_lock))


async def watch_for_connection(watchdog_queue, logger, timeout=config.TIMEOUT):
    while True:
        try:
            async with async_timeout.timeout(timeout) as cm:
                event = await watchdog_queue.get()
        except concurrent.futures.TimeoutError:
            pass
        timestamp = datetime.now().timestamp()
        if not cm.expired:
            message = f'[{timestamp}] Connection is alive. Source: {event}'
            logger.debug(message)
        else:
            message = f'[{timestamp}] {timeout}s timeout is elapsed'
            logger.debug(message)
            raise ConnectionError()


async def handle_connection(host, listener_port, sender_port, token,
                            messages_queue, file_queue, sending_queue,
                            status_updates_queue, watchdog_queue,
                            watchdog_logger, ping_interval):
    while True:
        try:
            async with create_handy_nursery() as nursery:
                nursery.start_soon(generate_msgs(host, listener_port, messages_queue,
                              file_queue, status_updates_queue,
                              watchdog_queue))
                nursery.start_soon(authorize_and_send_msgs(host, sender_port, token, sending_queue,
                                    status_updates_queue, watchdog_queue, ping_interval))
                nursery.start_soon(watch_for_connection(watchdog_queue, watchdog_logger))
        except ConnectionError:
            pass
        else:
            break


async def main():
    messages_queue = asyncio.Queue()
    sending_queue = asyncio.Queue()
    status_updates_queue = asyncio.Queue()
    file_queue = asyncio.Queue()
    watchdog_queue = asyncio.Queue()
    watchdog_logger = logging.getLogger('watchdog')
    try:
        with open(config.TOKEN_FILE_PATH, 'r') as f:
            token = f.readline()
    except FileNotFoundError:
        messagebox.showerror('Зарегистрируйтесь',
                             'Нужно зарегистрироваться')
    try:
        async with create_handy_nursery() as nursery:
            nursery.start_soon(
                handle_connection(host=config.DEFAULT_HOST,
                                  listener_port=config.DEFAULT_LISTENER_PORT,
                                  sender_port=config.DEFAULT_SENDER_PORT,
                                  token=token,
                                  messages_queue=messages_queue,
                                  file_queue=file_queue,
                                  sending_queue=sending_queue,
                                  status_updates_queue=status_updates_queue,
                                  watchdog_queue=watchdog_queue,
                                  watchdog_logger=watchdog_logger,
                                  ping_interval=config.PING_INTERVAL)
            )
            nursery.start_soon(
                gui.draw(messages_queue, sending_queue, status_updates_queue)
            )
            nursery.start_soon(save_messages(config.HISTORY_FILE_PATH, file_queue)
        )
    except InvalidTokenException:
        messagebox.showerror('Неверный токен', 'Проверьте токен, сервер его не узнал')
    except (KeyboardInterrupt, gui.TkAppClosed):
        print('exit')
        exit()


if __name__ == '__main__':
    asyncio.run(main())

