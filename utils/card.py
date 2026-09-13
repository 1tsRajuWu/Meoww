import io
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import aiohttp

async def fetch_image(url: str) -> Image.Image:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            data = await response.read()
            return Image.open(io.BytesIO(data)).convert("RGBA")

def round_corners(im: Image.Image, rad: int) -> Image.Image:
    circle = Image.new('L', (rad * 2, rad * 2), 0)
    draw = ImageDraw.Draw(circle)
    draw.ellipse((0, 0, rad * 2 - 1, rad * 2 - 1), fill=255)
    
    alpha = Image.new('L', im.size, 255)
    w, h = im.size
    alpha.paste(circle.crop((0, 0, rad, rad)), (0, 0))
    alpha.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
    alpha.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
    alpha.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
    
    im.putalpha(alpha)
    return im

def draw_pill(draw, text, x, y, font, bg_color, text_color):
    padding_x = 12
    padding_y = 6
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    
    w = text_w + (padding_x * 2)
    h = text_h + (padding_y * 2) + 4
    
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h//2, fill=bg_color)
    draw.text((x + padding_x, y + padding_y), text, fill=text_color, font=font)
    
    return w

async def generate_now_playing_card(
    title: str,
    artist: str,
    duration_str: str,
    requester: str,
    thumbnail_url: str
) -> io.BytesIO:
    # Card dimensions
    W, H = 800, 250
    
    # Background (Dark Theme like Amla)
    bg = Image.new('RGBA', (W, H), (20, 24, 34, 255))
    
    # Draw Background Gradient (Optional) or solid
    draw = ImageDraw.Draw(bg)
    draw.rounded_rectangle([0, 0, W, H], radius=20, fill=(26, 32, 44, 255), outline=(45, 55, 72, 255), width=2)

    # Load fonts
    try:
        font_title = ImageFont.truetype("assets/fonts/Roboto-Bold.ttf", 40)
        font_artist = ImageFont.truetype("assets/fonts/Roboto-Regular.ttf", 24)
        font_pill = ImageFont.truetype("assets/fonts/Roboto-Bold.ttf", 18)
    except IOError:
        font_title = ImageFont.load_default()
        font_artist = ImageFont.load_default()
        font_pill = ImageFont.load_default()

    # Load and place thumbnail
    if thumbnail_url:
        try:
            thumb = await fetch_image(thumbnail_url)
            # Crop to square
            tw, th = thumb.size
            size = min(tw, th)
            left = (tw - size)/2
            top = (th - size)/2
            right = (tw + size)/2
            bottom = (th + size)/2
            thumb = thumb.crop((left, top, right, bottom))
            thumb = thumb.resize((180, 180), Image.Resampling.LANCZOS)
            thumb = round_corners(thumb, 16)
            bg.paste(thumb, (35, 35), thumb)
        except Exception as e:
            print(f"Error loading thumbnail: {e}")

    # Texts
    # Title - truncate if too long
    max_title_len = 35
    display_title = title if len(title) <= max_title_len else title[:max_title_len-3] + "..."
    draw.text((240, 45), display_title, fill=(255, 255, 255, 255), font=font_title)
    
    # Artist
    max_artist_len = 45
    display_artist = artist if len(artist) <= max_artist_len else artist[:max_artist_len-3] + "..."
    draw.text((240, 95), display_artist, fill=(160, 174, 192, 255), font=font_artist)

    # Badges (Pills)
    pill_y = 165
    current_x = 240
    
    # Playing Badge
    w = draw_pill(draw, "▶ Playing", current_x, pill_y, font_pill, (49, 130, 206, 255), (255, 255, 255, 255))
    current_x += w + 10
    
    # Duration Badge
    w = draw_pill(draw, duration_str, current_x, pill_y, font_pill, (45, 55, 72, 255), (226, 232, 240, 255))
    current_x += w + 10
    
    # Requester Badge
    w = draw_pill(draw, f"Req: {requester}", current_x, pill_y, font_pill, (45, 55, 72, 255), (226, 232, 240, 255))

    # Watermark
    draw.text((W - 80, 20), "MEOW", fill=(255, 255, 255, 100), font=font_pill)

    # Save to buffer
    buffer = io.BytesIO()
    bg.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
