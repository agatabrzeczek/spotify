from dotenv import load_dotenv
import os
from flask import Flask, redirect, request, jsonify, session
import requests
import urllib.parse
from datetime import datetime
import time
import pickle
import sqlite3
import math
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, request, url_for, session, redirect
import json
import base64
from PIL import Image, ImageOps
from io import BytesIO
from flask import *

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
# CLIENT_ID = os.getenv("CLIENT_ID")
# CLIENT_SECRET = os.getenv("CLIENT_SECRET")
# REDIRECT_URI = os.getenv("REDIRECT_URI")

AUTH_URL = 'https://accounts.spotify.com/authorize'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
API_BASE_URL = 'https://api.spotify.com/v1/'

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    scope = 'user-library-read playlist-modify-public playlist-modify-private ugc-image-upload'

    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'scope': scope,
        'redirect_uri': REDIRECT_URI
    }

    auth_url = f'{AUTH_URL}?{urllib.parse.urlencode(params)}'

    return redirect(auth_url)

@app.route('/callback')
def callback():
    if 'error' in request.args:
        return jsonify({"error": request.args['error']})

    if 'code' in request.args:
        req_body = {
            'code': request.args['code'],
            'grant_type': 'authorization_code',
            'redirect_uri': REDIRECT_URI,
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET
        }

        response = requests.post(TOKEN_URL, data=req_body)
        token_info = response.json()

        session['access_token'] = token_info['access_token']
        session['refresh_token'] = token_info['refresh_token']
        session['expires_at'] = datetime.now().timestamp() + token_info['expires_in']

        return render_template('loading.html')

@app.route('/loading')
def loading():
    return render_template('loading.html')

@app.route('/sort-saved-songs')
def sort_saved_songs():
    if 'access_token' not in session:
        return redirect('/login')

    if datetime.now().timestamp() >= session['expires_at']:
        return redirect('/refresh-token')

    connection = sqlite3.connect('./spotify.db')
    cursor = connection.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS songs (id TEXT UNIQUE, title TEXT, artist_id TEXT, album_id TEXT, year INTEGER); ''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS artists (id TEXT UNIQUE, name TEXT, genre TEXT); ''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS genres (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, mapping_result TEXT); ''')
    connection.commit()

    cursor.execute('''DELETE FROM songs;''')
    connection.commit()

    cursor.execute('''DELETE FROM artists;''')
    connection.commit()

    songlist = get_saved_songs(cursor, connection)
    if (songlist == 401):
        return redirect('/refresh-token')
    if (songlist == 403):
        return render_template('unauthorized.html')

    artist_genres = get_artist_genres(cursor, connection)
    if (artist_genres == 401):
        return redirect('/refresh-token')

    update_genre_file(cursor, connection)
    songs_by_genre = group_songs_by_genre(cursor, connection)
    songs_by_decade = group_songs_by_decade(cursor, connection)
    playlist_ids = initialize_playlists(songs_by_genre, songs_by_decade)
    add_songs_to_playlists(songs_by_genre, playlist_ids)
    add_songs_to_playlists(songs_by_decade, playlist_ids)
    update_playlist_cover_arts(playlist_ids, cursor, connection)

    cursor.close()
    connection.close()

    if (artist_genres == None):
        return 'error'

    return render_template('done.html')

def get_saved_songs(cursor, connection):

    if (debug_local == False):
        time.sleep(30)

        headers = {
            'Authorization': f"Bearer {session['access_token']}"
        }

        i = 0
        while True:
            response = requests.get(API_BASE_URL + f'me/tracks?offset={i}&limit=50', headers=headers)
            try:
                tracks = response.json()
            except Exception as e:
                print(e)
                return 403

            if (pickling == True):
                with open("debug_files/debug_saved_songs.pickle", "wb") as debug_saved_tracks:
                    pickle.dump(tracks, debug_saved_tracks)
            add_tracks_to_database(tracks, cursor, connection)
            if (len(tracks['items']) < 50):
                break
            i += 50

    else:
            while True:
                try:
                    with open("debug_files/debug_saved_songs.pickle", "rb") as debug_saved_tracks:
                        tracks = pickle.load(debug_saved_tracks)
                        add_tracks_to_database(tracks, cursor, connection)
                except EOFError:
                    break

    status_code = get_song_years(tracks, cursor, connection)
    if status_code != 200:
        return 401

    return jsonify(tracks)

def add_tracks_to_database(songs, cursor, connection):
    for song in songs['items']:
        song_id = song['track']['id']
        title = song['track']['name']
        artist_id = song['track']['artists'][0]['id']
        album_id = song['track']['album']['id']
        sql_statement = f'''INSERT OR IGNORE INTO songs(id, title, artist_id, album_id) VALUES(?, ?, ?, ?);'''
        cursor.execute(sql_statement, (song_id, title, artist_id, album_id))
        connection.commit()

def get_song_years(songs, cursor, connection):

    headers = {
        'Authorization': f"Bearer {session['access_token']}"
    }

    cursor.execute("SELECT DISTINCT album_id FROM songs")
    data = []
    data = cursor.fetchall()
    album_ids = []
    for album in data:
        album_ids.append(album[0])

    amount = len(album_ids)
    no_of_batches = math.ceil(amount/20)

    for i in range(0, no_of_batches*20, 20):
        ids = ','.join(album_ids[i:(i+20)])
        response = requests.get(API_BASE_URL + f'albums?ids={ids}', headers=headers)
        albums = response.json()
        try:
            for album in albums['albums']:
                album_id = album['id']
                release_year = album['release_date'][:4]

                sql_statement = '''UPDATE songs SET year = ? WHERE album_id = ?'''
                cursor.execute(sql_statement, (release_year, album_id))
                connection.commit()
        except KeyError:
            return albums['error']['status']

    return 200

def get_artist_genres(cursor, connection):

    cursor.execute("SELECT DISTINCT artist_id FROM songs")
    artist_ids = []
    artist_ids = cursor.fetchall()

    if (debug_local == False):

        headers = {
            'Authorization': f"Bearer {session['access_token']}"
        }

        amount = len(artist_ids)
        no_of_batches = math.ceil(amount/50)

        for i in range(0, no_of_batches):
            ids_parameter = ''
            for id in artist_ids[(i*50):(i*50+49)]:
                ids_parameter += id[0] + '%2C'
            if (len(artist_ids[(i*50):(i*50+49)]) == 49):
                ids_parameter += artist_ids[(i*50+49)][0]

            response = requests.get(API_BASE_URL + f'artists?ids={ids_parameter}', headers=headers)
            #print(type(response))

            try:
                artists = response.json()
                if (pickling == True):
                    with open("debug_files/debug_artists.pickle", "wb") as debug_artists:
                        pickle.dump(artists, debug_artists)
            except ValueError:
                if (str(response) == '<Response [429]>'):
                    print('Too many requests')
                else:
                    print('Unexpected error')
                return None

            if 'error' not in artists:
                add_artists_to_database(artists, cursor, connection)
            else:
                return artists['error']['status']

    else:
        with open("debug_files/debug_artists.pickle", "rb") as debug_artists:
            while True:
                try:
                    artists = pickle.load(debug_artists)

                    if 'error' not in artists:
                        add_artists_to_database(artists, cursor, connection)
                    else:
                        return artists['error']['status']

                except EOFError:
                    break

    return jsonify(artists)

def add_artists_to_database(artists, cursor, connection):
    # cursor.execute("SELECT DISTINCT artist_id FROM songs")
    # artist_ids = []
    # artist_ids = cursor.fetchall()

    for artist in artists['artists']:
        if (artist != None): #arist is None when and of list is reached
            artist_id = artist['id']
            artist_name = artist['name']

            try:
                genre = artist['genres'][0]
            except IndexError:
                genre = None

            sql_statement = f'''INSERT OR IGNORE INTO artists(id, name, genre) VALUES(?, ?, ?);'''
            cursor.execute(sql_statement, (artist_id, artist_name, genre))
            connection.commit()

def update_genre_file(cursor, connection):
    cursor.execute("SELECT DISTINCT genre FROM artists")
    genres = []
    genres = cursor.fetchall()
    
    for item in genres:
        genre_name = item[0]
        if genre_name != None:
            sql_statement = f'''INSERT OR IGNORE INTO genres(name) VALUES(?);'''
            cursor.execute(sql_statement, (genre_name,))
            connection.commit()

def group_songs_by_genre(cursor, connection):

    cursor.execute("SELECT songs.id, genres.mapping_result FROM songs JOIN artists ON songs.artist_id = artists.id JOIN genres ON artists.genre = genres.name WHERE genres.mapping_result IS NOT NULL ORDER BY songs.id;")
    data = []
    data = cursor.fetchall()

    songs_by_genre = {}

    songs_by_genre['electronic'] = []
    songs_by_genre['pop'] = []
    songs_by_genre['rap'] = []
    songs_by_genre['rock'] = []

    for item in data:
            
        song_id = item[0]
        mapped_genre = item[1]

        songs_by_genre[mapped_genre].append('spotify:track:' + song_id)

    genres_to_be_removed = []

    for genre in songs_by_genre:
        if len(songs_by_genre[genre]) < 10:
            genres_to_be_removed.append(genre)

    for genre in genres_to_be_removed:  
        songs_by_genre.pop(genre)

    return songs_by_genre

    # add_songs_to_playlist(electronic_songs, playlist_ids['electronic'])
    # add_songs_to_playlist(pop_songs, playlist_ids['pop'])
    # add_songs_to_playlist(rap_songs, playlist_ids['rap'])
    # add_songs_to_playlist(rock_songs, playlist_ids['rock'])

    # for playlist_genre in playlist_ids:
    #     update_playlist_cover_art(playlist_ids[playlist_genre], playlist_genre, cursor, connection)

def group_songs_by_decade(cursor, connection):

    cursor.execute("SELECT id, year FROM songs;")
    data = []
    data = cursor.fetchall()

    songs_by_decade = {}

    for item in data:
            
        song_id = item[0]
        year = item[1]

        decade = str(year - (year % 10)) + 's'

        if decade not in songs_by_decade:
            songs_by_decade[decade] = []

        songs_by_decade[decade].append('spotify:track:' + song_id)

    decades_to_be_removed = []
    for decade in songs_by_decade:
        if len(songs_by_decade[decade]) < 10:
            decades_to_be_removed.append(decade)

    for decade in decades_to_be_removed:    
        songs_by_decade.pop(decade)

    return songs_by_decade
    # playlist_ids = initialize_playlists()

    # add_songs_to_playlist(electronic_songs, playlist_ids['electronic'])
    # add_songs_to_playlist(pop_songs, playlist_ids['pop'])
    # add_songs_to_playlist(rap_songs, playlist_ids['rap'])
    # add_songs_to_playlist(rock_songs, playlist_ids['rock'])

    # for playlist_genre in playlist_ids:
    #     update_playlist_cover_art(playlist_ids[playlist_genre], playlist_genre, cursor, connection)

def add_songs_to_playlists(song_grouping, playlist_ids):

    headers = {
        'Authorization': f"Bearer {session['access_token']}",
        'Content-Type': 'application/json'
    }

    for criterion in song_grouping:
        songlist = song_grouping[criterion]
        amount = len(songlist)
        no_of_batches = math.ceil(amount/100)

        uris = ','.join(songlist[0:100])

        now = datetime.now()

        body = {
            "description": f"updated {now.strftime('%Y-%m-%d %H:%M:%S')}"
        }

        response = requests.put(API_BASE_URL + f'playlists/{playlist_ids[criterion]}', headers=headers, json=body)

        response = requests.put(API_BASE_URL + f'playlists/{playlist_ids[criterion]}/tracks?uris={uris}', headers=headers)

        for i in range(100, no_of_batches*100, 100):
            uris = ','.join(songlist[i:(i+100)])
            response = requests.post(API_BASE_URL + f'playlists/{playlist_ids[criterion]}/tracks?uris={uris}', headers=headers)

def initialize_playlists(songs_by_genre, songs_by_decade):

    headers = {
        'Authorization': f"Bearer {session['access_token']}"
    }

    playlists = []

    playlist_ids = {}

    criterias = []

    for genre in songs_by_genre:
        criterias.append(genre)

    for decade in songs_by_decade:
        criterias.append(decade)

    for criterion in criterias:
        playlist_ids[criterion] = ''

    response = requests.get(API_BASE_URL + f'me/playlists', headers=headers)
    playlists = response.json()

    try:
        for playlist in playlists['items']:
            for playlist_criterion in PLAYLIST_NAMES:
                if (playlist['name'] == PLAYLIST_NAMES[playlist_criterion]):
                    playlist_ids[playlist_criterion] = playlist['id']
            for playlist_criterion in criterias:
                if playlist_ids[playlist_criterion] == '':
                    if playlist['name'] == f'Your {playlist_criterion} songs by Agata':
                        playlist_ids[playlist_criterion] = playlist['id']
    except KeyError:
        print(playlists)

    

    response = requests.get(API_BASE_URL + f'me', headers=headers)
    user_id = response.json()['id']

    headers['Content-Type'] = 'application/json'

    data = {}

    for playlist_criterion in playlist_ids:
        if (playlist_ids[playlist_criterion] == ''):
            if (playlist_criterion in PLAYLIST_NAMES):
                data['name'] = PLAYLIST_NAMES[playlist_criterion]
            else:
                data['name'] = f'Your {playlist_criterion} songs by Agata'
            response = requests.post(API_BASE_URL + f'users/{user_id}/playlists', headers=headers, json=data)
            playlist_ids[playlist_criterion] = response.json()['id']

    return playlist_ids

def update_playlist_cover_arts(playlist_ids, cursor, connection):

    headers = {
        'Authorization': f"Bearer {session['access_token']}"
    }

    for criterion in playlist_ids:
        try:
            cursor.execute(f"SELECT count(*), album_id FROM songs WHERE year >= {int(criterion[0:-1])} AND year < {int(criterion[0:-1]) + 10} GROUP BY album_id ORDER BY count(*) DESC;")
        except ValueError:
            cursor.execute(f"SELECT genres.mapping_result, songs.album_id, count(*) FROM songs JOIN artists ON songs.artist_id = artists.id JOIN genres ON artists.genre = genres.name WHERE genres.mapping_result = '{criterion}' group by songs.album_id ORDER BY count(*) desc;")

        data = []
        data = cursor.fetchone()

        album_id = data[1]

        response = requests.get(API_BASE_URL + f'albums/{album_id}', headers=headers)
        album = response.json()

        try:
            cover_art = Image.open(BytesIO(requests.get(album['images'][0]['url']).content))
        except KeyError:
            print(album)

        cover_art = ImageOps.grayscale(cover_art) 

        match criterion:
            case 'electronic':
                color = '#2d00e4'
            case 'pop':
                color = '#aa00e4'
            case 'rap':
                color = '#11443a'
            case 'rock':
                color = '#420f18'
            case '1960s':
                color = '#653200'
            case '1970s':
                color = '#031d41'
            case '1980s':
                color = '#ffbe00'
            case '1990s':
                color = '#ff4d00'
            case '2000s':
                color = '#0000ff'
            case '2010s':
                color = '#ff0b4f'
            case '2020s':
                color = '#ffffff'
            case _:
                color = '#00000000'

        color_canvas = Image.new('RGB', cover_art.size, color)
        cover_art = cover_art.convert(mode='RGB')
        cover_art = Image.blend(cover_art, color_canvas, 0.5)
        output = BytesIO()
        cover_art.save(output, format='JPEG') 
        cover_art_data = output.getvalue()
        body = base64.b64encode(cover_art_data)
        if not isinstance(body, str):
            body = body.decode()

        response = requests.put(API_BASE_URL + f'playlists/{playlist_ids[criterion]}/images', headers=headers, data=body)

@app.route('/refresh-token')
def refresh_token():
    if refresh_token not in session:
        return redirect('/login')

    if datetime.now().timestamp > session['expires_at']:
        req_body = {
            'grant_type': 'refresh_token',
            'refresh_token': session['refresh_token'],
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET
        }

        response = requests.post(TOKEN_URL, data=req_body)
        new_token_info = response.json()

        session['access_token'] = new_token_info['access_token']
        session['expires_at'] = datetime.now().timestamp() + new_token_info['expires_in']

        return redirect('/sort-saved-songs')

@app.route('/testing')
def testing():
    return render_template('testing.html')


debug_local = False
pickling = False

if __name__ == '__main__':

    CLIENT_ID = os.getenv("CLIENT_ID")
    CLIENT_SECRET = os.getenv("CLIENT_SECRET")
    REDIRECT_URI = os.getenv("REDIRECT_URI")

    app.run(host='0.0.0.0',
            port=80,
            debug=False)
