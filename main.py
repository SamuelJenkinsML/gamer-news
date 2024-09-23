import feedparser
import csv
import requests
import time
import fasthtml.common as fh
import asyncio
import os
import aiohttp

from datetime import datetime
from bs4 import BeautifulSoup
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

style = (
    fh.Style(
        """
    body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }
    h1 { color: #2c3e50; }
    .article { border: 1px solid #ddd; border-radius: 5px; padding: 15px; margin-bottom: 20px; }
    .article h2 { margin-top: 0; }
    .article img { max-width: 100%; height: auto; }
    .article a { color: #3498db; text-decoration: none; }
    .article a:hover { text-decoration: underline; }
"""
    ),
)

app = fh.FastHTML(hdrs=(style))
rt = app.route

db = fh.database("data/summaries.db")
summaries = db.t.summaries
if summaries not in db.t:
    summaries.create(
        url=str,
        title=str,
        summary=str,
        image_url=str,
        hn_comments=str,
        created_at=float,
        pk="url",
    )
Summary = summaries.dataclass()


def Article(s):
    return fh.Div(
        fh.H2(fh.A(s.title, href=s.url)),
        fh.Img(src=s.image_url) if s.image_url else None,
        fh.P(s.summary),
        cls="article",
    )


@rt("/")
async def get():
    latest_summaries = summaries(order_by="-created_at", limit=20)
    return fh.Title("GamerNews"), fh.Body(
        fh.H1("AI Summarised Eurogamer News"),
        fh.P("Front-page news articles summarised hourly."),
        *(Article(s) for s in latest_summaries),
        cls="container",
    )


async def parse_eurogamer_rss():
    url = "https://www.eurogamer.net/feed/news"
    feed = feedparser.parse(url)

    articles = []

    for entry in feed.entries[:30]:
        article = {
            "title": entry.title,
            "link": entry.link,
            "summary": entry.description,
            "published": entry.published,
            "author": entry.author if "author" in entry else "Unknown",
        }
        articles.append(article)

    return articles


def save_to_csv(articles):
    """Utility function for local testing"""
    filename = f"eurogamer_articles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(filename, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file, fieldnames=["title", "link", "summary", "published", "author"]
        )
        writer.writeheader()
        for article in articles:
            writer.writerow(article)

    print(f"Articles saved to {filename}")


async def scrape_article(url, session):
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.text, "html.parser")

        article_content = soup.find("div", class_="article_body")
        og_image = soup.find("meta", property="og:image")
        image_url = og_image["content"] if og_image else None

        if image_url:
            async with session.get(image_url, timeout=3) as image_response:
                if image_response.status != 200:
                    image_url = None

            print(f"Image URL: {image_url}")

        if article_content:
            # Extract text from paragraphs
            paragraphs = article_content.find_all("p")
            text = " ".join([p.get_text() for p in paragraphs])
            print(f"Text: {text}")
            return [text.strip(), image_url]
    except Exception as e:
        return f"Error scraping {url}: {str(e)}"


sp = """You are a helpful assistant that summarizes articles. Given an article text, possibly including unrelated scraping artefacts, return a summary of the article. If the text is just something like 'enable javascript' or 'turn off your ad blocker', just respond with "Could not summarize article." Otherwise, respond with just the summary (no preamble). Favour extremely conciseness and brevity. Start directly with the contents. Aim for <100 words."""


async def summarise_text(text):
    print(f"Summarizing text: {text}")
    chat_completion = client.chat.completions.create(
        messages=[
            {"role": "system", "content": sp},
            {"role": "user", "content": f"Please summarize the following text: {text}"},
        ],
        model="gpt-4o-mini",
    )

    summary = chat_completion.choices[0].message.content.strip()
    return summary


async def process_article(article):

    url = article["link"]

    existing = summaries(where=f"url='{url}'")
    if existing:
        return existing[0]

    async with aiohttp.ClientSession() as session:
        try:
            url = article["link"]
            text = await scrape_article(url, session)

            article["scraped_text"] = text[0]
            article["img_url"] = text[1]

            # small sleep for rate limiting
            time.sleep(1)

            summary = await summarise_text(article["scraped_text"])

            summaries.upsert(
                Summary(
                    url=url,
                    title=article["title"],
                    summary=summary,
                    image_url=article["img_url"],
                    created_at=time.time(),
                )
            )
        except aiohttp.ClientError as e:
            print(f"Network error occurred: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")


async def update_summaries():
    while True:
        try:
            articles = await parse_eurogamer_rss()
        except Exception:
            print("Error fetching news")
            await asyncio.sleep(3600)
            continue
        for article in articles:
            await process_article(article)

        await asyncio.sleep(3600)


@app.on_event("startup")
async def start_update_task():
    asyncio.create_task(update_summaries())


fh.serve()
