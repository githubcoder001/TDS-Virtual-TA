import json
from typing import List, Dict, Any
import os
from tqdm import tqdm
from openai import OpenAI
from pinecone import Pinecone, ServerlessSpec

# Initialize clients
openai_client = OpenAI(
    base_url="https://api.aiproxy.io/v1",
    api_key="eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6IjIyZjEwMDA4MTZAZHMuc3R1ZHkuaWl0bS5hYy5pbiJ9.e54s2pFBjHiqRbKmOSI4Xf2AeVbmw6NaBqPMOHTL8lI"
)


pinecone = Pinecone(api_key="YOUR_PINECONE_API_KEY_HERE")

# Initialize Pinecone index
index_name = "discourse-embeddings"
if index_name not in pinecone.list_indexes().names():
    pinecone.create_index(
        name=index_name,
        dimension=1536,  # OpenAI ada-002 dimension
        metric="cosine",
        spec=ServerlessSpec(
            cloud="aws",
            region="us-west-2"
        )
    )

index = pinecone.Index(index_name)

def process_posts(filename: str) -> Dict[int, Dict[str, Any]]:
    """Load and group posts by topic"""
    with open(filename, "r", encoding="utf-8") as f:
        posts_data = json.load(f)
    
    topics = {}
    for post in posts_data:
        topic_id = post["topic_id"]
        if topic_id not in topics:
            topics[topic_id] = {
                "topic_title": post.get("topic_title", ""),
                "posts": []
            }
        topics[topic_id]["posts"].append(post)
    
    # Sort posts by post_number
    for topic in topics.values():
        topic["posts"].sort(key=lambda p: p["post_number"])
    
    return topics

def build_thread_map(posts: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """Build reply tree structure"""
    thread_map = {}
    for post in posts:
        parent = post.get("reply_to_post_number")
        if parent not in thread_map:
            thread_map[parent] = []
        thread_map[parent].append(post)
    return thread_map

def extract_thread(root_num: int, posts: List[Dict[str, Any]], thread_map: Dict[int, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Extract full thread starting from root post"""
    thread = []
    
    def collect_replies(post_num):
        post = next(p for p in posts if p["post_number"] == post_num)
        thread.append(post)
        for reply in thread_map.get(post_num, []):
            collect_replies(reply["post_number"])
    
    collect_replies(root_num)
    return thread

def embed_and_index_threads(topics: Dict[int, Dict[str, Any]], batch_size: int = 100):
    """Embed threads using OpenAI and index in Pinecone"""
    vectors = []
    
    for topic_id, topic_data in tqdm(topics.items()):
        posts = topic_data["posts"]
        topic_title = topic_data["topic_title"]
        thread_map = build_thread_map(posts)
        
        # Process root posts (those without parents)
        root_posts = thread_map.get(None, [])
        for root_post in root_posts:
            thread = extract_thread(root_post["post_number"], posts, thread_map)
            
            # Combine thread text
            combined_text = f"Topic: {topic_title}\n\n"
            combined_text += "\n\n---\n\n".join(
                post["content"].strip() for post in thread
            )
            
            # Get embedding from OpenAI
            response = openai_client.embeddings.create(
                input=combined_text,
                model="text-embedding-3-small"
            )
            embedding = response.data[0].embedding
            
            # Prepare vector for Pinecone
            vector = {
                "id": f"{topic_id}_{root_post['post_number']}",
                "values": embedding,
                "metadata": {
                    "topic_id": topic_id,
                    "topic_title": topic_title,
                    "root_post_number": root_post["post_number"],
                    "post_numbers": [p["post_number"] for p in thread],
                    "combined_text": combined_text
                }
            }
            vectors.append(vector)
            
            # Batch upsert when we have enough vectors
            if len(vectors) >= batch_size:
                index.upsert(vectors=vectors)
                vectors = []
    
    # Upsert any remaining vectors
    if vectors:
        index.upsert(vectors=vectors)

def semantic_search(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Search for relevant threads using OpenAI embeddings"""
    # Get query embedding
    query_response = openai_client.embeddings.create(
        input=query,
        model="text-embedding-3-small"
    )
    query_embedding = query_response.data[0].embedding
    
    # Search Pinecone
    search_response = index.query(
        vector=query_embedding,
        top_k=top_k,
        include_metadata=True
    )
    
    results = []
    for match in search_response.matches:
        results.append({
            "score": match.score,
            "topic_id": match.metadata["topic_id"],
            "topic_title": match.metadata["topic_title"],
            "root_post_number": match.metadata["root_post_number"],
            "post_numbers": match.metadata["post_numbers"],
            "combined_text": match.metadata["combined_text"]
        })
    
    return results

def generate_answer(query: str, context_texts: List[str]) -> str:
    """Generate answer using OpenAI"""
    context = "\n\n---\n\n".join(context_texts)
    messages = [
        {"role": "system", "content": "You are a helpful assistant that answers questions based on forum discussions."},
        {"role": "user", "content": f"Based on these forum excerpts:\n\n{context}\n\nQuestion: {query}\n\nAnswer:"}
    ]
    
    response = openai_client.chat.completions.create(
        model="gpt-4-turbo-preview",
        messages=messages,
        temperature=0.7,
        max_tokens=500
    )
    
    return response.choices[0].message.content

# Example usage
if __name__ == "__main__":
    # Load and process data
    topics = process_posts("discourse_posts.json")
    print(f"Loaded {len(topics)} topics")
    
    # Index data (only needs to be done once)
    embed_and_index_threads(topics)
    print("Indexing complete")
    
    # Example search
    query = "If a student scores 10/10 on GA4 as well as a bonus, how would it appear on the dashboard?"
    results = semantic_search(query, top_k=3)
    
    print("\nTop search results:")
    for i, res in enumerate(results, 1):
        print(f"\n[{i}] Score: {res['score']:.4f}")
        print(f"Topic: {res['topic_title']}")
        print(f"Content snippet: {res['combined_text'][:500]}...\n")
    
    # Generate answer
    context_texts = [res["combined_text"] for res in results]
    answer = generate_answer(query, context_texts)
    print("\nGenerated Answer:\n", answer)