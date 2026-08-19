from .models import TextFeatures
from .utils import tokenize_words, tokenize_sentences, calculate_lexical_diversity, safe_divide


def extract_features(answer_text: str) -> TextFeatures:
    chars = len(answer_text)
    words = tokenize_words(answer_text)
    sentences = tokenize_sentences(answer_text)
    
    word_count = len(words)
    sentence_count = len(sentences)
    avg_sent_length = safe_divide(word_count, sentence_count, 0.0)
    
    lexical_div = calculate_lexical_diversity(words)
    
    abstraction_keywords = ['approach', 'methodology', 'framework', 'concept', 'strategy', 
                           'principle', 'generally', 'typically', 'usually', 'often']
    abstraction_score = sum(1 for w in words if w in abstraction_keywords) / max(word_count, 1)
    
    personal_keywords = ['i', 'my', 'we', 'our', 'me']
    personal_markers = sum(1 for w in words if w in personal_keywords)
    
    numeric_presence = sum(1 for w in words if any(c.isdigit() for c in w))
    
    # FIX: Store raw text in features
    return TextFeatures(
        char_count=chars,
        word_count=word_count,
        sentence_count=sentence_count,
        avg_sentence_length=avg_sent_length,
        lexical_diversity=lexical_div,
        abstraction_score=abstraction_score,
        personal_markers=personal_markers,
        numeric_presence=numeric_presence,
        raw_text=answer_text  # FIX: Preserve for downstream analysis
    )
