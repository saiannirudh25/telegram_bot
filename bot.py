import os
import logging
import requests
import pymongo
import textract
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import PyPDF2
from docx import Document
import pytesseract
from PIL import Image

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")

# Initialize MongoDB
client = pymongo.MongoClient(MONGO_URI)
db = client.telegram_bot
users_collection = db.users
chat_history_collection = db.chat_history
files_collection = db.files

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    existing_user = users_collection.find_one({"chat_id": chat_id})
    
    if not existing_user:
        users_collection.insert_one({"first_name": user.first_name, "username": user.username, "chat_id": chat_id})
        reply_markup = ReplyKeyboardMarkup([[KeyboardButton("Share Contact", request_contact=True)]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text("Please share your contact info to complete registration.", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Welcome back! You can start chatting now.")

async def contact_handler(update: Update, context: CallbackContext) -> None:
    contact = update.message.contact
    chat_id = update.effective_chat.id
    users_collection.update_one({"chat_id": chat_id}, {"$set": {"phone_number": contact.phone_number}})
    await update.message.reply_text("Thank you! Your contact info has been saved.")

async def gemini_chat(update: Update, context: CallbackContext) -> None:
    user_message = update.message.text
    chat_id = update.effective_chat.id

    # Retrieve previous conversation history
    chat_history = list(chat_history_collection.find({"chat_id": chat_id}).sort("_id", -1).limit(10))
    chat_history.reverse()  # Reverse to maintain chronological order

    # Prepare the conversation context for Gemini
    contents = []
    for msg in chat_history:
        contents.append({"role": "user", "parts": [{"text": msg["user_message"]}]})
        contents.append({"role": "model", "parts": [{"text": msg["bot_reply"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": contents
    }
    headers = {"Content-Type": "application/json"}
    
    response = requests.post(url, json=payload, headers=headers)
    response_json = response.json()

    # Debugging: Log API response
    logger.info(f"Gemini API Response: {response_json}")

    # Extract text from the response properly
    try:
        bot_reply = response_json["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        bot_reply = "Sorry, I couldn't process that."

    # Store the new message and bot reply in the chat history
    chat_history_collection.insert_one({"chat_id": chat_id, "user_message": user_message, "bot_reply": bot_reply})
    await update.message.reply_text(bot_reply)

async def handle_file(update: Update, context: CallbackContext) -> None:
    document = update.message.document
    file_id = document.file_id
    file_info = await context.bot.get_file(file_id)
    file_name = document.file_name
    file_url = file_info.file_path

    # Download the file
    file_path = f"downloads/{file_name}"
    await file_info.download_to_drive(file_path)

    # Extract text from the file
    try:
        text = extract_text_from_file(file_path)
    except Exception as e:
        logger.error(f"Error extracting text from file: {e}")
        await update.message.reply_text("Sorry, I couldn't extract text from the file.")
        return

    # Send the extracted text to Gemini for analysis
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": f"Analyze the following content:\n{text}"}]}]
    }
    headers = {"Content-Type": "application/json"}

    response = requests.post(url, json=payload, headers=headers)
    response_json = response.json()

    try:
        file_analysis = response_json["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        file_analysis = "No analysis available."

    files_collection.insert_one({"file_name": file_name, "analysis": file_analysis, "file_url": file_url})
    await update.message.reply_text(f"File received: {file_name}\nAnalysis: {file_analysis}")

async def web_search(update: Update, context: CallbackContext) -> None:
    if not context.args:
        await update.message.reply_text("Please provide a search query.")
        return

    query = " ".join(context.args)
    url = f"https://www.googleapis.com/customsearch/v1?q={query}&key={GOOGLE_CSE_ID}&cx={GOOGLE_CSE_ID}"
    
    try:
        search_response = requests.get(url)
        response_json = search_response.json()

        # Check if 'items' exist in the response
        if 'items' not in response_json:
            await update.message.reply_text("No results found or there was an issue with the search.")
            logger.error(f"Search API error: {response_json}")
            return

        # Log and format the search results
        logger.info(f"Web Search Response: {response_json}")

        results = response_json["items"]
        reply_text = "\n".join([f"{i+1}. {item['title']}: {item['link']}" for i, item in enumerate(results[:5])])
        await update.message.reply_text(reply_text if reply_text else "No results found.")
    except Exception as e:
        logger.error(f"Error during web search: {e}")
        await update.message.reply_text("An error occurred while performing the search.")


def extract_text_from_file(file_path):
    if file_path.endswith('.pdf'):
        # Extract text from PDF
        with open(file_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            text = ''.join([page.extract_text() for page in reader.pages])
        return text
    elif file_path.endswith('.docx'):
        # Extract text from Word document
        doc = Document(file_path)
        text = '\n'.join([para.text for para in doc.paragraphs])
        return text
    elif file_path.endswith(('.png', '.jpg', '.jpeg')):
        # Extract text from image using OCR
        image = Image.open(file_path)
        text = pytesseract.image_to_string(image)
        return text
    elif file_path.endswith('.txt'):
        # Extract text from plain text file
        with open(file_path, 'r', encoding='utf-8') as file:
            text = file.read()
        return text
    else:
        raise ValueError("Unsupported file format")
# Initialize bot
app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gemini_chat))
app.add_handler(MessageHandler(filters.ATTACHMENT, handle_file))
app.add_handler(CommandHandler("websearch", web_search))

app.run_polling()