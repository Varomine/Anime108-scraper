import os
import threading
import uuid
from flask import Flask, render_template, request, jsonify
from scraper import Anime108Scraper

app = Flask(__name__)

# Directory where files will be saved
DOWNLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'downloads'))
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# Global dictionary to track active downloads
# Format: { task_id: { 'status': 'idle|downloading|merging|completed|failed', 'progress': 0, 'total': 0, 'percentage': 0, 'speed': '', 'message': '', 'title': '', 'lang': '' } }
downloads_status = {}

def run_download_thread(task_id, url, lang):
    scraper = Anime108Scraper()
    
    def progress_callback(status, current, total, message):
        percentage = int((current / total) * 100) if total > 0 else 0
        downloads_status[task_id].update({
            'status': status,
            'progress': current,
            'total': total,
            'percentage': percentage,
            'message': message
        })
        
    try:
        downloads_status[task_id].update({
            'status': 'downloading',
            'message': 'Initiating connections and resolving streaming sources...'
        })
        
        # Resolve metadata first
        html = scraper.get_page_content(url)
        metadata = scraper.parse_show_page(html, url)
        
        downloads_status[task_id].update({
            'title': f"{metadata['title']} - Ep {metadata.get('episode', 1)}",
            'lang': lang
        })
        
        # Download
        result = scraper.download_video(
            episode_url=url,
            output_dir=DOWNLOAD_DIR,
            lang=lang,
            concurrency=16,
            progress_callback=progress_callback
        )
        
        downloads_status[task_id].update({
            'status': 'completed',
            'message': f"Successfully downloaded and saved to: {os.path.basename(result['filepath'])}"
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        downloads_status[task_id].update({
            'status': 'failed',
            'message': f"Error: {str(e)}"
        })

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/parse', methods=['POST'])
def api_parse():
    data = request.get_json() or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL is required'}), 400
        
    try:
        scraper = Anime108Scraper()
        html = scraper.get_page_content(url)
        metadata = scraper.parse_show_page(html, url)
        
        # Format the response
        response_data = {
            'title': metadata.get('title'),
            'post_id': metadata.get('post_id'),
            'current_episode': metadata.get('episode'),
            'episodes': metadata.get('episodes', {'Thai': [], 'Sound Track': []})
        }
        return jsonify(response_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/player-url', methods=['POST'])
def api_player_url():
    data = request.get_json() or {}
    url = data.get('url')
    lang = data.get('lang', 'Sound Track')
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
        
    try:
        scraper = Anime108Scraper()
        html = scraper.get_page_content(url)
        metadata = scraper.parse_show_page(html, url)
        
        iframe_url = scraper.get_player_iframe(
            post_id=metadata['post_id'],
            episode=metadata['episode'],
            server=metadata['server'],
            lang=lang,
            title=metadata['title']
        )
        return jsonify({'iframe_url': iframe_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/docs')
def docs():
    return render_template('docs.html')

@app.route('/search', methods=['GET'])
def api_search():
    keyword = request.args.get('keyword') or request.args.get('q') or ''
    if not keyword:
        return jsonify({'error': 'Query parameter "keyword" or "q" is required'}), 400
        
    try:
        scraper = Anime108Scraper()
        results = scraper.search_anime(keyword)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json() or {}
    url = data.get('url')
    lang = data.get('lang', 'Sound Track') # Default: Subbed
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
        
    task_id = str(uuid.uuid4())
    downloads_status[task_id] = {
        'status': 'idle',
        'progress': 0,
        'total': 0,
        'percentage': 0,
        'message': 'Waiting to start...',
        'title': 'Fetching show info...',
        'lang': lang,
        'url': url
    }
    
    # Start thread
    thread = threading.Thread(target=run_download_thread, args=(task_id, url, lang))
    thread.daemon = True
    thread.start()
    
    return jsonify({'task_id': task_id})

@app.route('/api/progress/<task_id>', methods=['GET'])
def api_progress(task_id):
    status = downloads_status.get(task_id)
    if not status:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(status)

@app.route('/api/downloads', methods=['GET'])
def api_list_downloads():
    # Return list of downloaded files in the folder
    files = []
    if os.path.exists(DOWNLOAD_DIR):
        for f in os.listdir(DOWNLOAD_DIR):
            if f.endswith('.mp4'):
                path = os.path.join(DOWNLOAD_DIR, f)
                files.append({
                    'filename': f,
                    'size': f"{os.path.getsize(path) / (1024*1024):.1f} MB",
                    'path': path
                })
    return jsonify({'downloads': files})

if __name__ == '__main__':
    print(f"Starting Anime108 Scraper local server at http://localhost:5000")
    print(f"Videos will be downloaded to: {DOWNLOAD_DIR}")
    app.run(host='0.0.0.0', port=5000, debug=False)
