import gradio as gr
from openai import OpenAI
from typing import List, Tuple, Dict
import os
import logging
import json
from datetime import datetime
import numpy as np
from dotenv import load_dotenv, find_dotenv
import openai
import traceback
import tiktoken
from rank_bm25 import BM25Okapi
import re

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Load .env file
load_dotenv(find_dotenv())

# Initialize OpenAI client
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OpenAI API key is not set. Please set the OPENAI_API_KEY environment variable.")
client = OpenAI(api_key=api_key)

# Create results folder
RESULTS_FOLDER = "results"
os.makedirs(RESULTS_FOLDER, exist_ok=True)

# Available embedding models
EMBEDDING_MODELS = ["text-embedding-ada-002", "text-embedding-3-small", "text-embedding-3-large"]

def save_json_file(data: Dict, file_prefix: str, folder: str, max_tokens: int):
    """Save data as a JSON file in the specified folder with date and max_tokens in the filename"""
    current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{file_prefix}_{current_time}_{str(max_tokens)}.json"
    file_path = os.path.join(folder, file_name)
    
    os.makedirs(folder, exist_ok=True)
    
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=2)
    logging.info(f"Data saved to {file_path}")

def generate_question(entity: str) -> str:
    prompt = f"Generate an interesting and insightful question about {entity}. The question should be a complete sentence."
    response = client.chat.completions.create(
        model="gpt-4o-mini-2024-07-18",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that generates insightful questions."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=50,
        n=1,
        temperature=0.7,
    )
    
    return response.choices[0].message.content.strip()

def num_tokens_from_string(string: str, model_name: str) -> int:
    encoding = tiktoken.encoding_for_model(model_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens

def extend_answer(entity: str, query: str, current_answer: str, min_tokens: int, max_tokens: int) -> str:
    current_tokens = num_tokens_from_string(current_answer, "gpt-4o-mini-2024-07-18")
    
    if current_tokens >= min_tokens:
        return current_answer
    
    additional_tokens_needed = min_tokens - current_tokens
    prompt = f"The following is a partial answer to the question '{query}' about {entity}. Please extend this answer with additional relevant information. Add approximately {additional_tokens_needed} tokens:\n\n{current_answer}"
    
    response = client.chat.completions.create(
        model="gpt-4o-mini-2024-07-18",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that extends answers with additional relevant information."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=max_tokens - current_tokens,
        n=1,
        temperature=0.5,
    )
    
    extended_answer = current_answer + " " + response.choices[0].message.content.strip()
    
    return extend_answer(entity, query, extended_answer, min_tokens, max_tokens)

def generate_answer(entity: str, query: str, max_tokens: int) -> str:
    min_tokens = max(50, max_tokens - 100)
    
    prompt = f"Answer the following question about {entity} concisely: {query}"
    response = client.chat.completions.create(
        model="gpt-4o-mini-2024-07-18",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that provides concise answers to questions."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=max_tokens,
        n=1,
        temperature=0.5,
    )
    
    initial_answer = response.choices[0].message.content.strip()
    return extend_answer(entity, query, initial_answer, min_tokens, max_tokens)

def vectorize(text: str, model: str) -> List[float]:
    try:
        response = client.embeddings.create(
            input=[text],
            model=model
        )
        return response.data[0].embedding
    except Exception as e:
        logging.error(f"Error in vectorize function: {str(e)}")
        logging.error(f"Input text: {text}")
        logging.error(f"Model: {model}")
        raise

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

def preprocess_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^\w\s\']', '', text)
    text = ' '.join(text.split())
    return text

def calculate_bm25_scores(queries: Dict, corpus: Dict) -> Tuple[Dict[str, Dict[str, float]], Dict]:
    query_texts = [item['text'] for item in queries.values()]
    answer_texts = [item['text'] for item in corpus.values()]
    
    preprocessed_queries = [preprocess_text(text) for text in query_texts]
    preprocessed_answers = [preprocess_text(text) for text in answer_texts]
    
    tokenized_answers = [answer.split() for answer in preprocessed_answers]
    bm25 = BM25Okapi(tokenized_answers)

    scores = {}
    best_matches = {}
    for query_key, query in zip(queries.keys(), preprocessed_queries):
        query_tokens = query.split()
        query_scores = bm25.get_scores(query_tokens)
        scores[query_key] = {f"answer_{i+1}": float(score) for i, score in enumerate(query_scores)}
        
        best_answer_key = max(scores[query_key], key=scores[query_key].get)
        best_score = scores[query_key][best_answer_key]
        best_matches[query_key] = {
            "query_text": queries[query_key]["text"],
            "best_answer_key": best_answer_key,
            "best_answer_text": corpus[best_answer_key]["text"],
            "bm25_score": best_score
        }
    
    return scores, best_matches

def find_best_matches(queries_data, corpus_data, embedding_results):
    best_matches = {}
    for model, similarities in embedding_results.items():
        best_matches[model] = {}
        for query_key, query_similarities in similarities.items():
            best_answer_key = max(query_similarities, key=query_similarities.get)
            best_similarity = query_similarities[best_answer_key]
            best_matches[model][query_key] = {
                "query_text": queries_data[query_key]["text"],
                "best_answer_key": best_answer_key,
                "best_answer_text": corpus_data[best_answer_key]["text"],
                "cosine_similarity": best_similarity
            }
    return best_matches

def process_entities(entities: List[str], max_tokens: int) -> Tuple[Dict, Dict, Dict, Dict, Dict, Dict]:
    # Create a results folder with all entity names
    RESULTS_FOLDER = f"results_{'_'.join([e.replace(' ', '_') for e in entities])}"
    os.makedirs(RESULTS_FOLDER, exist_ok=True)

    queries_data = {}
    corpus_data = {}

    for i, entity in enumerate(entities, 1):
        query = generate_question(entity)
        queries_data[f"query_{i}"] = {"text": query, "entity": entity}
        corpus_data[f"answer_{i}"] = {"text": generate_answer(entity, query, max_tokens), "entity": entity}
    
    # Save queries and corpus
    save_json_file({"entities": entities, "queries": queries_data}, "queries", RESULTS_FOLDER, max_tokens)
    save_json_file({"entities": entities, "corpus": corpus_data}, "corpus", RESULTS_FOLDER, max_tokens)
    
    # Process for each embedding model
    embedding_results = {}
    for model in EMBEDDING_MODELS:
        model_folder = os.path.join(RESULTS_FOLDER, model)
        os.makedirs(model_folder, exist_ok=True)

        corpus_vectors = {
            key: {
                "text": data["text"],
                "vector": vectorize(data["text"], model),
                "entity": data["entity"]
            }
            for key, data in corpus_data.items()
        }
        queries_vectors = {
            key: {
                "text": data["text"],
                "vector": vectorize(data["text"], model),
                "entity": data["entity"]
            }
            for key, data in queries_data.items()
        }
        
        cosine_similarities = {}
        for query_key, query_data in queries_vectors.items():
            cosine_similarities[query_key] = {}
            for answer_key, answer_data in corpus_vectors.items():
                similarity = cosine_similarity(query_data["vector"], answer_data["vector"])
                cosine_similarities[query_key][answer_key] = similarity
        
        embedding_results[model] = cosine_similarities
        
        # Save cosine similarities
        save_json_file({"entities": entities, "cosine_similarities": cosine_similarities}, 
                       "cosine_similarities", model_folder, max_tokens)
        
        # Save vector data with associated text
        save_json_file({"entities": entities, "corpus_vectors": corpus_vectors, "query_vectors": queries_vectors},
                       "vectors", model_folder, max_tokens)
    
    # Process for BM25
    bm25_folder = os.path.join(RESULTS_FOLDER, "BM25")
    os.makedirs(bm25_folder, exist_ok=True)
    bm25_scores, bm25_best_matches = calculate_bm25_scores(queries_data, corpus_data)
    save_json_file({"entities": entities, "scores": bm25_scores}, "scores", bm25_folder, max_tokens)
    save_json_file({"entities": entities, "best_matches": bm25_best_matches}, "best_matches", bm25_folder, max_tokens)
    
    # Find and save best matches for each model
    best_matches = find_best_matches(queries_data, corpus_data, embedding_results)
    for model, model_best_matches in best_matches.items():
        model_folder = os.path.join(RESULTS_FOLDER, model)
        save_json_file({"entities": entities, "best_matches": model_best_matches}, 
                       "best_matches", model_folder, max_tokens)
    
    return queries_data, corpus_data, embedding_results, bm25_scores, best_matches, bm25_best_matches

def integrated_interface(entity1: str, entity2: str, entity3: str, entity4: str, entity5: str, entity6: str, max_tokens: int) -> Tuple[str, str, str, str, str]:
    try:
        best_matches_summary = ""
        entities = [entity1, entity2, entity3, entity4, entity5, entity6]
        logging.info(f"Processing entities: {entities} with max tokens: {max_tokens}")
        
        queries_data, corpus_data, embedding_results, bm25_scores, best_matches, bm25_best_matches = process_entities(entities, max_tokens)
        
        queries_and_answers_text = f"Generated Questions and Answers for entities (max tokens: {max_tokens}):\n\n"
        for query_key, query_data in queries_data.items():
            answer_key = f"answer_{query_key.split('_')[1]}"
            queries_and_answers_text += f"Entity: {query_data['entity']}\n"
            queries_and_answers_text += f"{query_key}: {query_data['text']}\n"
            queries_and_answers_text += f"{answer_key}: {corpus_data[answer_key]['text']}\n\n"
        
        embedding_summary = "\nEmbedding-based Similarities:\n"
        for model, similarities in embedding_results.items():
            embedding_summary += f"\n{model}:\n"
            for query_key, query_similarities in similarities.items():
                embedding_summary += f"{query_key} (Entity: {queries_data[query_key]['entity']}):\n"
                for answer_key, similarity in query_similarities.items():
                    embedding_summary += f"  {answer_key} (Entity: {corpus_data[answer_key]['entity']}): {similarity:.4f}\n"
                embedding_summary += "\n"
        
        bm25_summary = "\nBM25 Scores:\n"
        for query_key, scores in bm25_scores.items():
            bm25_summary += f"{query_key} (Entity: {queries_data[query_key]['entity']}):\n"
            for answer_key, score in scores.items():
                bm25_summary += f"  {answer_key} (Entity: {corpus_data[answer_key]['entity']}): {score:.4f}\n"
            bm25_summary += "\n"
        
        best_matches_summary += "\nBM25 Best Matches:\n"
        for query_key, match_data in bm25_best_matches.items():
            best_matches_summary += f"{query_key} (Entity: {queries_data[query_key]['entity']}):\n"
            best_matches_summary += f"  Query: {match_data['query_text']}\n"
            best_matches_summary += f"  Best Answer ({match_data['best_answer_key']}, Entity: {corpus_data[match_data['best_answer_key']]['entity']}): {match_data['best_answer_text']}\n"
            best_matches_summary += f"  BM25 Score: {match_data['bm25_score']:.4f}\n\n"

        logging.info("Entity processing completed successfully")
        return queries_and_answers_text, embedding_summary, bm25_summary, best_matches_summary, json.dumps({"entities": entities, "embedding_results": embedding_results, "bm25_scores": bm25_scores, "best_matches": best_matches, "bm25_best_matches": bm25_best_matches}, indent=2)
    except Exception as e:
        error_message = f"An unexpected error occurred: {str(e)}"
        logging.error(error_message)
        logging.error(traceback.format_exc())
        return error_message, "", "", "", "{}"
    
iface = gr.Interface(
    fn=integrated_interface,
    inputs=[
        gr.Textbox(label="Entity 1"),
        gr.Textbox(label="Entity 2"),
        gr.Textbox(label="Entity 3"),
        gr.Textbox(label="Entity 4"),
        gr.Textbox(label="Entity 5"),
        gr.Textbox(label="Entity 6"),
        gr.Slider(minimum=50, maximum=500, step=10, label="Max Tokens for Answer", value=100)
    ],
    outputs=[
        gr.Textbox(label="Generated Questions and Answers"),
        gr.Textbox(label="Embedding-based Similarities Summary"),
        gr.Textbox(label="BM25 Scores Summary"),
        gr.Textbox(label="Best Matches Summary"),
        gr.JSON(label="Detailed Results")
    ],
    title="Integrated RAG Model with Multiple Entities, Embedding Models, BM25, and Best Matches",
    description="Enter 6 entities and set the maximum tokens for answers. This will generate a question for each entity, provide answers, calculate similarities using multiple embedding models, perform BM25 scoring, and find the best matches based on cosine similarity."
)

if __name__ == "__main__":
    iface.launch(debug=True)