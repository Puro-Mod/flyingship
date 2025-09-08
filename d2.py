import asyncio
import websockets
import json
import uuid
import random
import math
from collections import deque # Импортируем deque для чата

# --- КОНСТАНТЫ ---
PLAYER_SPEED = 0.1
SHIP_SPEED = 3
TILE_SIZE = 40
SHIP_GRID_WIDTH = 10
SHIP_GRID_HEIGHT = 10
GRAVITY = 0.01
JUMP_STRENGTH = -0.15
INVENTORY_SIZE = 5
MAX_CHAT_MESSAGES = 10 # Максимальное количество сообщений в истории чата

# --- НОВЫЕ КОНСТАНТЫ: ГРАНИЦЫ МИРА ---
WORLD_BOUNDS = {
    "x_min": -2000, "x_max": 2000,
    "y_min": -2000, "y_max": 2000
}

# --- Глобальное состояние игры ---
GAME_STATE = {"players": {}, "ships": {}}
CONNECTED_CLIENTS = set()
PUBLIC_SHIP_ID = None # Эта переменная больше не используется для выбора конкретного корабля

# Функция для генерации читаемого ID корабля
def generate_short_id():
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return ''.join(random.choice(chars) for _ in range(8))


def create_default_ship_grid():
    grid = [[0 for _ in range(SHIP_GRID_WIDTH)] for _ in range(SHIP_GRID_HEIGHT)]
    for y in range(SHIP_GRID_HEIGHT):
        for x in range(SHIP_GRID_WIDTH):
            if y == 0 or y == SHIP_GRID_HEIGHT - 1 or x == 0 or x == SHIP_GRID_WIDTH - 1:
                grid[y][x] = 1
    grid[SHIP_GRID_HEIGHT - 1][0] = 2
    grid[0][SHIP_GRID_WIDTH - 1] = 2
    return grid

async def game_logic_loop():
    while True:
        # Player physics
        for player_id, player in GAME_STATE["players"].items():
            if player.get("piloting"): player['vx'], player['vy'] = 0, 0; continue
            ship = GAME_STATE["ships"].get(player["ship_id"]);
            if not ship: continue
            next_x = player['x'] + player['vx']; player_y_grid = int(player['y'] - 0.5)
            if 0 <= player_y_grid < SHIP_GRID_HEIGHT:
                left_bound_grid, right_bound_grid = int(next_x - 0.4), int(next_x + 0.4)
                is_colliding = False
                if player['vx'] < 0 and (left_bound_grid < 0 or ship['grid'][player_y_grid][left_bound_grid] > 0): is_colliding = True; player['x'] = left_bound_grid + 1.4
                elif player['vx'] > 0 and (right_bound_grid >= SHIP_GRID_WIDTH or ship['grid'][player_y_grid][right_bound_grid] > 0): is_colliding = True; player['x'] = right_bound_grid - 0.4
                if is_colliding: player['vx'] = 0
                else: player['x'] = next_x
            player['vy'] += GRAVITY; next_y = player['y'] + player['vy']
            player_x_grid = int(player['x']); player['is_on_ground'] = False
            if 0 <= player_x_grid < SHIP_GRID_WIDTH:
                head_grid_y, feet_grid_y = int(next_y - 1), int(next_y)
                if player['vy'] < 0 and (head_grid_y < 0 or ship['grid'][head_grid_y][player_x_grid] > 0): player['vy'] = 0; player['y'] = head_grid_y + 2
                elif player['vy'] > 0 and (feet_grid_y >= SHIP_GRID_HEIGHT or ship['grid'][feet_grid_y][player_x_grid] > 0): player['vy'] = 0; player['y'] = feet_grid_y; player['is_on_ground'] = True
                else: player['y'] = next_y
        
        # --- Ship physics (ИСПРАВЛЕНА ПРОВЕРКА ГРАНИЦ) ---
        for ship_id, ship in GAME_STATE["ships"].items():
            ship['world_x'] += ship['vx']
            ship['world_y'] += ship['vy']
            
            half_width = (ship['width'] * TILE_SIZE) / 2
            half_height = (ship['height'] * TILE_SIZE) / 2

            # Проверка и ограничение по X
            if ship['world_x'] - half_width < WORLD_BOUNDS['x_min']:
                ship['world_x'] = WORLD_BOUNDS['x_min'] + half_width
            elif ship['world_x'] + half_width > WORLD_BOUNDS['x_max']:
                ship['world_x'] = WORLD_BOUNDS['x_max'] - half_width
            
            # Проверка и ограничение по Y
            if ship['world_y'] - half_height < WORLD_BOUNDS['y_min']:
                ship['world_y'] = WORLD_BOUNDS['y_min'] + half_height
            elif ship['world_y'] + half_height > WORLD_BOUNDS['y_max']:
                ship['world_y'] = WORLD_BOUNDS['y_max'] - half_height


        await asyncio.sleep(1/60)

async def broadcast_game_state():
    clients_to_broadcast = set(CONNECTED_CLIENTS)
    if clients_to_broadcast:
        # Сериализуем игровое состояние
        message = json.dumps(GAME_STATE, default=lambda o: list(o) if isinstance(o, deque) else o)
        
        # Также отправляем список доступных кораблей для лобби
        available_ships = []
        for ship_id, ship in GAME_STATE["ships"].items():
            # Здесь можно добавить логику для определения, "публичный" ли корабль
            # В данном случае, все корабли считаются "публичными" для выбора
            available_ships.append({
                "id": ship_id,
                "short_id": ship.get("short_id"), # Отправляем короткий ID
                "name": ship.get("name", "Безымянный корабль"),
                "players_count": sum(1 for p in GAME_STATE["players"].values() if p["ship_id"] == ship_id)
            })
        lobby_message = json.dumps({"type": "lobby_update", "availableShips": available_ships})

        tasks = []
        for client in clients_to_broadcast:
            # Отправляем полное игровое состояние только если игрок уже в игре
            if hasattr(client, 'player_id') and client.player_id in GAME_STATE["players"]:
                tasks.append(client.send(message))
            else: # Иначе отправляем только обновление лобби
                tasks.append(client.send(lobby_message))
        
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(1/60)

async def broadcast_loop_task():
    while True: await broadcast_game_state()

async def handler(websocket):
    client_id = str(uuid.uuid4())
    player_id = None; nickname = "Player"
    websocket.player_id = None # Добавляем player_id к websocket объекту для отслеживания состояния клиента
    print(f"[+] Новое подключение: {client_id}")
    CONNECTED_CLIENTS.add(websocket) # Добавляем клиента сразу, чтобы он мог получать обновления лобби

    try:
        init_message_str = await websocket.recv()
        init_data = json.loads(init_message_str)
        nickname = init_data.get('nickname', 'Player')[:15] or "Player" # Ограничение длины ника
        ship_id = None
        
        if init_data.get('type') == 'create_ship':
            player_id, ship_id = str(uuid.uuid4()), str(uuid.uuid4())
            ship_name = init_data.get('shipName', 'Безымянный корабль')[:20] # Ограничение длины названия корабля
            ship_short_id = generate_short_id() # Генерируем короткий ID
            
            spawn_pos = (0, 0)
            is_valid_spawn = False
            spawn_attempts = 0
            while not is_valid_spawn and spawn_attempts < 100:
                spawn_pos = (random.randint(WORLD_BOUNDS['x_min'] + 200, WORLD_BOUNDS['x_max'] - 200), 
                             random.randint(WORLD_BOUNDS['y_min'] + 200, WORLD_BOUNDS['y_max'] - 200))
                is_valid_spawn = True
                for other_ship_id, other_ship in GAME_STATE["ships"].items():
                    dist = math.hypot(other_ship['world_x'] - spawn_pos[0], other_ship['world_y'] - spawn_pos[1])
                    if dist < TILE_SIZE * SHIP_GRID_WIDTH * 1.5:
                        is_valid_spawn = False
                        break
                spawn_attempts += 1
            GAME_STATE["ships"][ship_id] = {
                "world_x": spawn_pos[0], "world_y": spawn_pos[1], "vx": 0, "vy": 0, "width": SHIP_GRID_WIDTH, "height": SHIP_GRID_HEIGHT,
                "pilot": None, "components": {"helm": {"x": 5, "y": SHIP_GRID_HEIGHT - 2}}, "grid": create_default_ship_grid(),
                "thrust_dir_x": 0, "thrust_dir_y": 0, "items_on_grid": [{"id": str(uuid.uuid4()), "type": "cargo_hatch", "x": 1, "y": SHIP_GRID_HEIGHT - 2}],
                "chat_messages": deque(maxlen=MAX_CHAT_MESSAGES), # Инициализация чата
                "name": ship_name, # Добавляем имя корабля
                "short_id": ship_short_id # Добавляем короткий ID
            }
            GAME_STATE["ships"][ship_id]["chat_messages"].append({"nick": "System", "text": f"Корабль '{ship_name}' ({ship_short_id}) создан."})
            print(f"[+] Игрок '{nickname}' создал корабль {ship_name} ({ship_id})")
        
        elif init_data.get('type') == 'join_ship': # Изменено с 'join_public' на 'join_ship'
            target_ship_id = init_data.get('shipId')
            if target_ship_id and target_ship_id in GAME_STATE["ships"]:
                player_id, ship_id = str(uuid.uuid4()), target_ship_id
                print(f"[+] Игрок '{nickname}' присоединился к кораблю {GAME_STATE['ships'][ship_id]['name']} ({ship_id})")
            else:
                await websocket.send(json.dumps({"error": "Выбранный корабль не существует или недоступен."})); return
        
        if player_id and ship_id:
            GAME_STATE["players"][player_id] = {
                "x": SHIP_GRID_WIDTH / 2, "y": SHIP_GRID_HEIGHT - 2, "vx": 0, "vy": 0, "nickname": nickname, "ship_id": ship_id,
                "piloting": False, "is_on_ground": False, "color": f"hsl({random.randint(0, 360)}, 100%, 75%)",
                "inventory": [None] * INVENTORY_SIZE
            }
            if ship_id in GAME_STATE["ships"]:
                 GAME_STATE["ships"][ship_id]["chat_messages"].append({"nick": "System", "text": f"Игрок '{nickname}' присоединился."})
            
            websocket.player_id = player_id # Присваиваем player_id к websocket объекту
            init_payload = {"type": "init_player", "playerId": player_id, "initialState": GAME_STATE}
            await websocket.send(json.dumps(init_payload, default=lambda o: list(o) if isinstance(o, deque) else o))
            
        else: # Если игрок не присоединился к кораблю, он остаётся в лобби и ждет обновлений лобби
            pass # Нет необходимости отправлять сообщение об ошибке здесь, так как лобби будет получать обновления
            
        async for message in websocket:
            if not player_id: continue # Игнорируем сообщения, если игрок еще не вошел в игру
            data = json.loads(message)
            player = GAME_STATE.get("players", {}).get(player_id)
            if not player: continue
            ship = GAME_STATE.get("ships", {}).get(player['ship_id'])
            if not ship: continue
            action_type = data.get('type')
            if action_type == 'input':
                keys = data.get('keys', {})
                if player['piloting']:
                    ship['vx'], ship['vy'], ship['thrust_dir_x'], ship['thrust_dir_y'] = 0, 0, 0, 0
                    if keys.get("up"): ship['vy'] = -SHIP_SPEED; ship['thrust_dir_y'] = 1 
                    if keys.get("down"): ship['vy'] = SHIP_SPEED; ship['thrust_dir_y'] = -1
                    if keys.get("left"): ship['vx'] = -SHIP_SPEED; ship['thrust_dir_x'] = 1 
                    if keys.get("right"): ship['vx'] = SHIP_SPEED; ship['thrust_dir_x'] = -1 
                else:
                    if keys.get("left"): player['vx'] = -PLAYER_SPEED
                    elif keys.get("right"): player['vx'] = PLAYER_SPEED
                    else: player['vx'] = 0
                    if keys.get("up") and player['is_on_ground']: player['vy'] = JUMP_STRENGTH
            elif action_type == 'interact':
                item_picked_up = False
                for i, item in enumerate(ship.get("items_on_grid", [])):
                    dist_sq = (player['x'] - (item['x']+0.5))**2 + (player['y'] - (item['y']+1))**2
                    if dist_sq < 2**2:
                        for j in range(INVENTORY_SIZE):
                            if player['inventory'][j] is None: player['inventory'][j] = ship['items_on_grid'].pop(i); item_picked_up = True; break
                        if item_picked_up: break
                if not item_picked_up:
                    helm = ship['components']['helm']
                    dist_sq = (player['x'] - helm['x'])**2 + (player['y'] - helm['y'])**2
                    if player['piloting']:
                        player['piloting'] = False; ship['pilot'] = None; ship['vx'], ship['vy'] = 0, 0
                        ship['thrust_dir_x'] = 0; ship['thrust_dir_y'] = 0
                    elif dist_sq < 2**2 and not ship['pilot']:
                        player['piloting'] = True; ship['pilot'] = player_id
            elif action_type == 'place_item':
                slot_index = data.get('slot')
                grid_x, grid_y = data.get('x'), data.get('y')
                if slot_index is not None and player['inventory'][slot_index] is not None:
                    if 0 <= grid_x < SHIP_GRID_WIDTH and 0 <= grid_y < SHIP_GRID_HEIGHT:
                        if ship['grid'][grid_y][grid_x] == 0:
                            item = player['inventory'][slot_index]
                            if item['type'] == 'cargo_hatch':
                                ship['grid'][grid_y][grid_x] = 3
                                player['inventory'][slot_index] = None
            # --- НОВЫЙ ОБРАБОТЧИК: ЧАТ ---
            elif action_type == 'send_chat':
                chat_text = data.get('text', '').strip()
                if 0 < len(chat_text) <= 100: # Ограничение на длину сообщения
                     ship['chat_messages'].append({'nick': player['nickname'], 'text': chat_text})

    except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK):
        print(f"[-] Соединение {client_id} закрыто.")
    finally:
        if websocket in CONNECTED_CLIENTS: CONNECTED_CLIENTS.remove(websocket)
        if player_id and player_id in GAME_STATE["players"]:
            player = GAME_STATE["players"][player_id]
            ship_id = player.get("ship_id")
            if ship_id and ship_id in GAME_STATE["ships"]:
                 GAME_STATE["ships"][ship_id]["chat_messages"].append({"nick": "System", "text": f"Игрок '{nickname}' отключился."})

            if player.get('piloting'):
                ship = GAME_STATE.get("ships", {}).get(player['ship_id'])
                if ship: ship['pilot'] = None; ship['vx'], ship['vy'] = 0, 0
                ship['thrust_dir_x'] = 0; ship['thrust_dir_y'] = 0
            del GAME_STATE["players"][player_id]
            print(f"[-] Игрок '{nickname}' ({player_id}) удален.")

async def main():
    print("Запуск игрового цикла..."); asyncio.create_task(game_logic_loop())
    print("Запуск рассылки..."); asyncio.create_task(broadcast_loop_task())
    async with websockets.serve(handler, "localhost", 8765):
        print("Сервер запущен на ws://localhost:8765"); await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())