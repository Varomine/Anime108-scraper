import os
import re
import sys
import argparse
import urllib.parse
from bs4 import BeautifulSoup
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import subprocess

class Anime108Scraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        })
        self.base_url = 'https://www.anime108.com'

    def get_page_content(self, url):
        """Fetch the page HTML content."""
        print(f"Fetching URL: {url}")
        response = self.session.get(url)
        response.raise_for_status()
        response.encoding = 'utf-8'
        return response.text

    def parse_show_page(self, html, url):
        """
        Parse the main show page or episode page.
        Extracts post_id, title, and all available episodes.
        """
        soup = BeautifulSoup(html, 'html.parser')
        metadata = {}

        # Try to find halim_cfg in scripts
        cfg_match = re.search(r'var halim_cfg\s*=\s*(\{[\s\S]*?\});', html)
        if cfg_match:
            try:
                # Basic parsing of JS object to Python dict (keys can be quoted or unquoted)
                cfg_str = cfg_match.group(1)
                # Ensure keys are double-quoted for JSON load, or extract via regex
                post_id_match = re.search(r'"post_id"\s*:\s*(\d+)', cfg_str)
                if not post_id_match:
                    post_id_match = re.search(r'post_id\s*:\s*(\d+)', cfg_str)
                
                if post_id_match:
                    metadata['post_id'] = int(post_id_match.group(1))
                
                episode_match = re.search(r'"episode"\s*:\s*(\d+)', cfg_str)
                if not episode_match:
                    episode_match = re.search(r'episode\s*:\s*(\d+)', cfg_str)
                if episode_match:
                    metadata['episode'] = int(episode_match.group(1))
                else:
                    metadata['episode'] = 1

                server_match = re.search(r'"server"\s*:\s*(\d+)', cfg_str)
                if not server_match:
                    server_match = re.search(r'server\s*:\s*(\d+)', cfg_str)
                if server_match:
                    metadata['server'] = int(server_match.group(1))
                else:
                    metadata['server'] = 1
            except Exception as e:
                print(f"Error parsing halim_cfg: {e}")

        # Fallbacks for post_id if script parsing failed
        if 'post_id' not in metadata:
            link_rel = soup.find('link', rel='shortlink')
            if link_rel and 'href' in link_rel.attrs:
                post_id_match = re.search(r'\?p=(\d+)', link_rel.attrs['href'])
                if post_id_match:
                    metadata['post_id'] = int(post_id_match.group(1))

        # Extract Title
        title_tag = soup.find('h1')
        if title_tag:
            metadata['title'] = title_tag.get_text().strip()
        else:
            metadata['title'] = soup.title.get_text().strip() if soup.title else "Anime Video"

        # Clean title for filename
        metadata['clean_title'] = re.sub(r'[\\/*?:"<>|]', '', metadata['title']).strip()

        # Find episode lists for Subbed (Sound Track / sequel_select_en) and Dubbed (Thai / sequel_select_th)
        episodes = {'Thai': [], 'Sound Track': []}
        
        # Dubbed (Thai)
        th_select = soup.find('select', id='sequel_select_th')
        if th_select:
            for option in th_select.find_all('option'):
                ep_url = option.get('value')
                if ep_url:
                    if not ep_url.startswith('http'):
                        ep_url = self.base_url + ep_url
                    episodes['Thai'].append({
                        'title': option.get_text().strip(),
                        'url': ep_url
                    })
        
        # Subbed (Sound Track / English / sequel_select_en)
        en_select = soup.find('select', id='sequel_select_en')
        if en_select:
            for option in en_select.find_all('option'):
                ep_url = option.get('value')
                if ep_url:
                    if not ep_url.startswith('http'):
                        ep_url = self.base_url + ep_url
                    episodes['Sound Track'].append({
                        'title': option.get_text().strip(),
                        'url': ep_url
                    })

        metadata['episodes'] = episodes
        return metadata

    def get_player_iframe(self, post_id, episode=1, server=1, lang='Sound Track', title=''):
        """Call the get.php API to retrieve the player iframe URL."""
        api_url = f'{self.base_url}/api/get.php'
        
        # We try to mimic the web client exactly
        data = {
            'action': 'halim_ajax_player',
            'nonce': '',  # Nonce is often empty as verified
            'episode': str(episode),
            'server': str(server),
            'postid': str(post_id),
            'lang': lang,
            'title': title
        }
        
        headers = {
            'Origin': self.base_url,
            'Referer': f'{self.base_url}/',
            'X-Requested-With': 'XMLHttpRequest'
        }
        
        print(f"Requesting player API for post_id: {post_id}, episode: {episode}, lang: {lang}...")
        response = self.session.post(api_url, headers=headers, data=data)
        response.raise_for_status()
        
        # The response is HTML containing an iframe
        html = response.text.replace('\\', '')
        iframe_match = re.search(r'src="([^"]+)"', html)
        if iframe_match:
            iframe_url = iframe_match.group(1)
            # Ensure it is absolute
            if iframe_url.startswith('//'):
                iframe_url = 'https:' + iframe_url
            return iframe_url
        
        raise Exception(f"Failed to find player iframe in API response: {response.text}")

    def resolve_stream_url(self, iframe_url):
        """Fetch iframe, extract ID, download master playlist, and find high-res stream."""
        parsed_url = urllib.parse.urlparse(iframe_url)
        params = urllib.parse.parse_qs(parsed_url.query)
        video_id = params.get('id', [None])[0]
        
        if not video_id:
            raise Exception(f"No video ID found in iframe URL: {iframe_url}")
            
        print(f"Resolved Video ID: {video_id}")
        
        # Base domain of the player
        player_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        # Fetch the master playlist (try both newplaylist and newplaylist_g)
        master_url = f"{player_domain}/newplaylist/{video_id}/{video_id}.m3u8"
        headers = {
            'Referer': iframe_url,
            'Origin': player_domain
        }
        
        print(f"Fetching master playlist from: {master_url}")
        res = self.session.get(master_url, headers=headers)
        if res.status_code != 200:
            # Fallback to newplaylist_g
            master_url = f"{player_domain}/newplaylist_g/{video_id}/{video_id}.m3u8"
            print(f"Master playlist failed, trying fallback: {master_url}")
            res = self.session.get(master_url, headers=headers)
            res.raise_for_status()
            
        # Parse master playlist
        playlist_content = res.text
        print("Master playlist content resolved successfully.")
        
        # Look for resolution lines
        # Example format:
        # #EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080
        # /m3u8/dc293979261c3a1b852d6e2e/dc293979261c3a1b852d6e2e438.m3u8
        lines = playlist_content.split('\n')
        streams = []
        current_res = "Unknown"
        for line in lines:
            line = line.strip()
            if line.startswith('#EXT-X-STREAM-INF'):
                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                if res_match:
                    current_res = res_match.group(1)
            elif line and not line.startswith('#'):
                streams.append({
                    'resolution': current_res,
                    'path': line
                })
                current_res = "Unknown"
                
        if not streams:
            raise Exception("No streams found in master playlist.")
            
        # Choose highest resolution
        # Usually 1080p is preferred
        streams.sort(key=lambda s: int(s['resolution'].split('x')[0]) if 'x' in s['resolution'] else 0, reverse=True)
        selected_stream = streams[0]
        print(f"Available resolutions: {[s['resolution'] for s in streams]}")
        print(f"Selected highest resolution: {selected_stream['resolution']} at path {selected_stream['path']}")
        
        # Test sub-playlist path
        sub_playlist_path = selected_stream['path']
        sub_url = f"{player_domain}{sub_playlist_path}"
        
        # Check if we need to modify path (e.g. replacing m3u8_g with m3u8 as verified in research)
        print(f"Checking stream playlist: {sub_url}")
        res_sub = self.session.get(sub_url, headers=headers)
        if res_sub.status_code == 200 and "Error" not in res_sub.text:
            return sub_url, iframe_url
            
        # If it returned Error (commonly seen with _g variant), try replacing m3u8_g with m3u8
        if "m3u8_g" in sub_playlist_path:
            alt_path = sub_playlist_path.replace("m3u8_g", "m3u8")
            alt_url = f"{player_domain}{alt_path}"
            print(f"Stream returned error. Trying fallback path: {alt_url}")
            res_sub_alt = self.session.get(alt_url, headers=headers)
            if res_sub_alt.status_code == 200 and "Error" not in res_sub_alt.text:
                return alt_url, iframe_url
                
        # Raise error if both failed
        raise Exception(f"Failed to fetch a valid sub-playlist from {sub_url} (Response: {res_sub.text})")

    def get_segments(self, stream_url, iframe_url):
        """Fetch the stream playlist and extract segment URLs."""
        headers = {
            'Referer': iframe_url
        }
        res = self.session.get(stream_url, headers=headers)
        res.raise_for_status()
        
        lines = res.text.split('\n')
        segments = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                # Handle relative or absolute URLs
                if line.startswith('http'):
                    segments.append(line)
                else:
                    # Resolve relative to stream url
                    segments.append(urllib.parse.urljoin(stream_url, line))
                    
        return segments

    def download_segment(self, url, idx, temp_dir, iframe_url, max_retries=3):
        """Download a single segment with retry logic."""
        filename = f"{idx:05d}.aaa"
        filepath = os.path.join(temp_dir, filename)
        
        headers = {
            'Referer': iframe_url,
            'User-Agent': self.session.headers['User-Agent']
        }
        
        for attempt in range(max_retries):
            try:
                # If file exists and size is non-zero, assume success
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    return filepath
                    
                response = self.session.get(url, headers=headers, timeout=15)
                if response.status_code == 200:
                    with open(filepath, 'wb') as f:
                        f.write(response.content)
                    return filepath
            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"\n[Warning] Failed to download segment {idx} (Attempt {attempt+1}/{max_retries}): {e}")
        return None

    def merge_segments(self, downloaded_files, output_filepath):
        """Concatenate MPEG-TS segments into a single file."""
        print(f"Merging {len(downloaded_files)} segments...")
        
        # Sort files to ensure correct ordering
        downloaded_files.sort()
        
        # Check if ffmpeg is available
        has_ffmpeg = False
        try:
            subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            has_ffmpeg = True
        except Exception:
            print("FFmpeg not found in PATH. Merging segments via direct binary concatenation.")
            
        temp_merged_ts = output_filepath + ".temp.ts"
        
        # Direct concatenation to a temp TS file
        with open(temp_merged_ts, 'wb') as outfile:
            for filepath in downloaded_files:
                if filepath and os.path.exists(filepath):
                    with open(filepath, 'rb') as infile:
                        outfile.write(infile.read())
                        
        if has_ffmpeg:
            print("Running FFmpeg to package stream cleanly into MP4 container...")
            try:
                # Run ffmpeg copy command to remux TS to MP4 without re-encoding
                cmd = [
                    'ffmpeg', '-y',
                    '-i', temp_merged_ts,
                    '-c', 'copy',
                    output_filepath
                ]
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode == 0:
                    print(f"Successfully created MP4 using FFmpeg: {output_filepath}")
                    if os.path.exists(temp_merged_ts):
                        os.remove(temp_merged_ts)
                    return True
                else:
                    print(f"FFmpeg error: {result.stderr.decode('utf-8')}")
            except Exception as e:
                print(f"Error running FFmpeg: {e}")
                
        # Fallback/default: Just rename the merged TS file to the target filepath
        print("Finalizing file container...")
        if os.path.exists(output_filepath):
            os.remove(output_filepath)
        os.rename(temp_merged_ts, output_filepath)
        print(f"Saved merged video file to: {output_filepath}")
        return True

    def download_video(self, episode_url, output_dir='downloads', lang='Sound Track', concurrency=16, check_only=False, progress_callback=None):
        """Full download workflow for an episode URL."""
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        # Step 1: Parse show page
        html = self.get_page_content(episode_url)
        metadata = self.parse_show_page(html, episode_url)
        
        print("\n--- Show Metadata ---")
        print(f"Title: {metadata['title']}")
        print(f"Post ID: {metadata.get('post_id')}")
        print(f"Episode: {metadata.get('episode')}")
        print(f"Server: {metadata.get('server')}")
        print("---------------------\n")
        
        if not metadata.get('post_id'):
            raise Exception("Could not find post ID on the page.")
            
        # Step 2: Get player iframe
        iframe_url = self.get_player_iframe(
            post_id=metadata['post_id'],
            episode=metadata['episode'],
            server=metadata['server'],
            lang=lang,
            title=metadata['title']
        )
        print(f"Iframe URL: {iframe_url}")
        
        # Step 3: Resolve stream URL
        stream_url, resolved_iframe = self.resolve_stream_url(iframe_url)
        print(f"Resolved Stream PlayList: {stream_url}")
        
        if check_only:
            print("[Check-Only] Finished checks. Playlist resolves correctly.")
            return {
                'success': True,
                'metadata': metadata,
                'stream_url': stream_url
            }
            
        # Step 4: Get segments
        segments = self.get_segments(stream_url, resolved_iframe)
        total_segments = len(segments)
        print(f"Total video segments to download: {total_segments}")
        
        if total_segments == 0:
            raise Exception("No segments found in the playlist.")
            
        # Step 5: Download segments concurrently
        temp_dir = os.path.join(output_dir, f"temp_{metadata['post_id']}_ep{metadata['episode']}_{lang.replace(' ', '')}")
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
            
        print("Downloading segments...")
        downloaded_files = [None] * total_segments
        completed_count = 0
        
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            # Map index to segment url
            futures = {
                executor.submit(self.download_segment, url, idx, temp_dir, resolved_iframe): idx 
                for idx, url in enumerate(segments)
            }
            
            # Wrap in tqdm progress bar
            for future in tqdm(as_completed(futures), total=total_segments, desc="Downloading Chunks"):
                idx = futures[future]
                filepath = future.result()
                if filepath:
                    downloaded_files[idx] = filepath
                completed_count += 1
                if progress_callback:
                    try:
                        progress_callback('downloading', completed_count, total_segments, f"Downloading chunk {completed_count}/{total_segments}")
                    except Exception:
                        pass
                    
        # Verify all segments are downloaded
        missing_segments = [i for i, f in enumerate(downloaded_files) if f is None]
        if missing_segments:
            print(f"[Warning] Failed to download {len(missing_segments)} segments: {missing_segments}")
            # Try once more for missing segments sequentially
            print("Retrying missing segments...")
            for idx in missing_segments:
                filepath = self.download_segment(segments[idx], idx, temp_dir, resolved_iframe, max_retries=5)
                downloaded_files[idx] = filepath
                completed_count = sum(1 for f in downloaded_files if f is not None)
                if progress_callback:
                    try:
                        progress_callback('downloading', completed_count, total_segments, f"Retrying chunk downloads: {completed_count}/{total_segments}")
                    except Exception:
                        pass
                
        # Check again
        still_missing = [i for i, f in enumerate(downloaded_files) if f is None]
        if still_missing:
            raise Exception(f"Failed to download the video because {len(still_missing)} segments could not be fetched.")
            
        # Step 6: Merge segments
        # Format output filename
        safe_title = metadata['clean_title']
        output_filename = f"{safe_title} - Ep {metadata['episode']} ({lang}).mp4"
        output_filepath = os.path.join(output_dir, output_filename)
        
        if progress_callback:
            try:
                progress_callback('merging', total_segments, total_segments, "Merging segments into output MP4 file...")
            except Exception:
                pass
                
        self.merge_segments(downloaded_files, output_filepath)
        
        # Clean up temporary directory files
        print("Cleaning up temporary chunk files...")
        for filepath in downloaded_files:
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass
            
        if progress_callback:
            try:
                progress_callback('completed', total_segments, total_segments, f"Saved final MP4 to: {output_filepath}")
            except Exception:
                pass
                
        print("Download workflow completed successfully!")
        return {
            'success': True,
            'filepath': output_filepath,
            'metadata': metadata
        }

    def search_anime(self, keyword):
        """Search anime108.com for a keyword and return results."""
        search_url = f"{self.base_url}/search_movie"
        # The search endpoint takes standard HTTP GET query
        headers = {
            'Referer': self.base_url + '/'
        }
        res = self.session.get(search_url, params={'keyword': keyword}, headers=headers)
        res.raise_for_status()
        res.encoding = 'utf-8'
        
        soup = BeautifulSoup(res.text, 'html.parser')
        boxes = soup.find_all('div', class_='box')
        results = []
        
        for box in boxes:
            link_tag = box.find('a')
            if link_tag:
                href = link_tag.get('href')
                if href and not href.startswith('http'):
                    href = self.base_url + href
                    
                img_tag = box.find('img')
                # Check for lazy loading sources
                img_src = None
                if img_tag:
                    img_src = img_tag.get('data-lazy-src') or img_tag.get('src')
                    if img_src and img_src.startswith('data:image'):
                        img_src = img_tag.get('src')
                        
                title_div = box.find('div', class_='p2')
                title = title_div.get_text().strip() if title_div else (img_tag.get('alt') if img_tag else "No Title")
                
                ep_tag = box.find('span', class_='EP') or box.find('span', class_='update')
                ep_text = ep_tag.get_text().strip() if ep_tag else ""
                
                results.append({
                    'title': title,
                    'url': href,
                    'image': img_src,
                    'episodes_info': ep_text
                })
        return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Anime108 Video Downloader")
    parser.add_argument('--url', required=True, help="URL of the anime page (e.g., https://www.anime108.com/mushen-ji-ep-2/)")
    parser.add_argument('--dir', default='downloads', help="Output directory for downloaded videos")
    parser.add_argument('--lang', default='Sound Track', choices=['Sound Track', 'Thai'], 
                        help="Language selection: 'Sound Track' (Subbed) or 'Thai' (Dubbed). Default: 'Sound Track'")
    parser.add_argument('--threads', type=int, default=16, help="Number of download threads. Default: 16")
    parser.add_argument('--check-only', action='store_true', help="Check playlist resolution without downloading")
    
    args = parser.parse_args()
    
    scraper = Anime108Scraper()
    try:
        scraper.download_video(
            episode_url=args.url,
            output_dir=args.dir,
            lang=args.lang,
            concurrency=args.threads,
            check_only=args.check_only
        )
    except Exception as e:
        print(f"\n[Error] {e}")
        sys.exit(1)
