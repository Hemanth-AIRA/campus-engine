import re
from bs4 import BeautifulSoup
from collections import Counter

def analyze_report(filepath):
    with open(filepath, 'r') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
    
    rows = soup.find_all('tr')[1:] # Skip header
    q2_skills_vs_questions = []
    langchain_mentions = 0
    total_q3_to_q10 = 0
    
    for row in rows:
        cols = row.find_all('td')
        if not cols or len(cols) < 5: continue
        
        q_num = cols[0].text.strip()
        question = cols[1].text.strip()
        skills = cols[4].text.strip()
        
        if q_num == '2':
            q2_skills_vs_questions.append((skills, question[:100] + "..."))
            
        if q_num in [str(i) for i in range(3, 11)]:
            total_q3_to_q10 += 1
            if 'langchain' in question.lower() or 'langgraph' in question.lower() or 'langserve' in question.lower():
                langchain_mentions += 1
                
    print("=== Q2 BEHAVIOR (Monolithic Responses API) ===")
    lora_q2 = sum(1 for _, q in q2_skills_vs_questions if 'lora' in q.lower() or 'qlora' in q.lower())
    print(f"Q2s asking about LoRA/QLoRA: {lora_q2} / {len(q2_skills_vs_questions)}")
    for s, q in q2_skills_vs_questions[:3]:
        print(f"Skills: {s}\nQuestion: {q}\n")
        
    print("\n=== Q3-Q10 BEHAVIOR (New Chat Completions Planner) ===")
    print(f"Mentions of LangChain/LangGraph/LangServe: {langchain_mentions} / {total_q3_to_q10} ({(langchain_mentions/max(1, total_q3_to_q10))*100:.1f}%)")

analyze_report('interview_simulation_report_v6.html')
