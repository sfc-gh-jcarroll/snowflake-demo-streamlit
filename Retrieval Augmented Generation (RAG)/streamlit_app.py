from io import BytesIO
from snowflake.cortex import Complete
from snowflake.snowpark import Session
from snowflake.snowpark.context import get_active_session
from typing import Dict, List
import pypdfium2 as pdfium
import streamlit as st

st.set_page_config(layout="wide")

session: Session = get_active_session()


@st.cache_data(show_spinner=False)
def get_files_summaries(_session: Session) -> Dict[str, str]:
    """
    Get files summaries using a stored procedure.
    """
    # Get PDF names
    summaries_dataframe = _session.sql(
        "SELECT DOC_NAME, DOC_CONTENT FROM DOCS_SUMMARIES;"
    ).to_pandas()

    summaries_dictionary = dict(
        zip(
            summaries_dataframe["DOC_NAME"].astype(str),
            summaries_dataframe["DOC_CONTENT"].astype(str),
        )
    )

    return summaries_dictionary


@st.cache_data()
def get_pdf_bytes(_session: Session, file_name: str) -> bytes:
    """
    Get the RAW content of a specific PDF file in byte format.
    """
    query = f"SELECT GET_PDF_RAW(BUILD_SCOPED_FILE_URL(@RAG_DEMO, '{file_name}'))"
    bytes = _session.sql(query).to_pandas().iloc[0, 0]
    return bytes


def view_pdf_page(session: Session, file_name: str) -> None:
    """
    Render a selected PDF page as an image.
    """
    # Gets PDF bytes, then transformed into a more usable object such as `PdfDocument`.
    file_byte = get_pdf_bytes(session, file_name)
    pdf = pdfium.PdfDocument(BytesIO(file_byte))

    # Get the number of pages to render a select to choose from.
    page_num_arr = {}
    for page_number in range(0, len(pdf)):
        page_num_arr["Page " + str(page_number + 1)] = page_number

    selected_page = st.selectbox(
        "Select the page that you want to see:", page_num_arr.keys()
    )

    # Render the selected page of the PDF.
    page = pdf.get_page(page_num_arr[selected_page])
    pil_image = page.render(scale=300 / 72).to_pil()
    with st.container(height=345, border=False):
        st.image(pil_image, use_column_width="always")


def display_pdf_summary(session: Session) -> None:
    """
    Display PDF summary based on selection and render a selected PDF page.
    """
    summaries_dictionary = get_files_summaries(session)

    st.write("**PDF Information:**")

    selected_summary = st.selectbox(
        "Select the PDF that you want to summarize:", summaries_dictionary.keys()
    )

    summary_tab, view_tab = st.tabs(["**Summary**", "**View PDF**"])
    with summary_tab:
        st.write(summaries_dictionary[selected_summary])

    with view_tab:
        view_pdf_page(session, selected_summary)


def get_similar_chunks_query(question: str, num_chunks: int) -> str:
    """
    Get similarity bewteen the question and the information
    available in the PDFs.
    """
    return f"""
            WITH RESULTS AS (
                SELECT
                    RELATIVE_PATH,
                    VECTOR_COSINE_SIMILARITY(
                        DOCS_CHUNKS_TABLE.CHUNK_VEC,
                        SNOWFLAKE.CORTEX.EMBED_TEXT_768('e5-base-v2', '{question.replace("'", "")}')
                    ) AS SIMILARITY,
                    CHUNK
                FROM
                    DOCS_CHUNKS_TABLE
                ORDER BY
                    SIMILARITY DESC
                LIMIT
                    {num_chunks}
            )
            SELECT
                CHUNK,
                RELATIVE_PATH
            FROM
                RESULTS;
            """


def get_similar_chunks(session: Session, question: str) -> str:
    """
    Executes the SQL query generated by get_similar_chunks_query and retrieves the most similar text chunks for a given question.
    """
    num_chunks = 3  # Num-chunks provided as context. Play with this to check how it affects your accuracy.
    chunks = session.sql(get_similar_chunks_query(question, num_chunks)).to_pandas()
    chunks_length = len(chunks) - 1
    similar_chunks = ""
    for i in range(0, chunks_length):
        similar_chunks += str(
            chunks.iloc[i, 0]
        )  # Gets chunks values from the dataframe (Chunks is the first column).

    return similar_chunks


def init_messages():
    """
    Initialize chat history
    """
    default_questions = """
        Some questions you may want to ask:
        1. What are some safety precautions for biking?
        2. How do I choose the right ski boots?
        3. Can you share some maintenance tips for my bike?
        4. What materials are downhill bike frames made of?
    """
    if "messages" not in st.session_state:
        st.session_state.messages = []
        st.session_state.messages.append(
            {"role": "assistant", "content": default_questions}
        )


def get_chat_history() -> List[str]:
    """
    Get the history from the `st.session_stage.messages` according to the slide window parameter
    """
    chat_history = []
    slide_window = 7  # how many last conversations to remember.
    start_index = max(0, len(st.session_state.messages) - slide_window)
    for i in range(start_index, len(st.session_state.messages) - 1):
        chat_history.append(st.session_state.messages[i])

    return chat_history


def summarize_question_with_history(
    chat_history: List[str], question: str, model: str
) -> str:
    """
    To get the right context, use the LLM to first summarize the previous conversation
    This will be used to get embeddings and find similar chunks in the docs for context
    """
    prompt = f"""
            Based on the chat history below and the question, generate a query that extend the question
            with the chat history provided. The query should be in natural language. 
            Answer with only the query. Do not add any explanation.
            
            <chat_history>
            {chat_history}
            </chat_history>
            <question>
            {question}
            </question>
            """

    summary = Complete(model, prompt)

    return summary


def get_cortex_prompt(question: str, model: str, session: Session) -> str:
    """
    Creates cortex prompt, based on the given text but also keeping in mind the coversation context.
    """
    chat_history = get_chat_history()

    if chat_history != []:
        question_summary = summarize_question_with_history(
            chat_history, question, model
        )
        prompt_context = get_similar_chunks(session, question_summary)
    else:
        prompt_context = get_similar_chunks(
            session, question
        )  # First question when using history

    prompt = f"""
           You are an expert chat assistance that extracts information from the CONTEXT provided
           between <context> and </context> tags.
           You offer a chat experience considering the information included in the CHAT HISTORY
           provided between <chat_history> and </chat_history> tags.
           When answering the question contained between <question> and </question> tags
           be concise and do not hallucinate. 
           If you don't have the information just say so.
           
           Do not mention the CONTEXT used in your answer.
           Do not mention the CHAT HISTORY used in your answer.
           
           <chat_history>
           {chat_history}
           </chat_history>
           <context>          
           {prompt_context}
           </context>
           <question>  
           {question}
           </question>
           Answer: 
           """

    return prompt


st.title(f"Retrieval Augmented Generation 💬")
st.write(
    """
    Introducing our smart chat assistant app! 📚💬
    Using the Retrieval Augmented Generation (RAG) process, the AI will read through your documents to provide you with accurate and contextually relevant responses.
    Whether you have questions about product details or need troubleshooting help, our app ensures you get the right answers.
    """
)
summary_col, chat_col = st.columns(2)

with summary_col:
    with st.container(border=True, height=650):
        with st.spinner("Generating summaries of your PDFs..."):
            display_pdf_summary(session)
with chat_col:
    st.write("**Chat with Document Assistant:**")
    init_messages()

    with st.expander("**Chat Settings**"):
        model_name = st.selectbox(
            "Select your model:",
            (
                "mixtral-8x7b",
                "snowflake-arctic",
                "mistral-large",
                "llama3-8b",
                "llama3-70b",
                "reka-flash",
                "mistral-7b",
                "llama2-70b-chat",
                "gemma-7b",
            ),
        )
    messages_container = st.container(height=495)

    # Display chat messages from history on app rerun
    for message in st.session_state.messages:
        with messages_container.chat_message(message["role"]):
            st.markdown(message["content"])

    # Accept user input
    if question := st.chat_input("What do you want to know about your documents?"):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": question})

        # Display user message in chat message container
        with messages_container.chat_message("user"):
            st.markdown(question)

        # Display assistant response in chat message container
        with messages_container.chat_message("assistant"):
            response = ""
            question = question.replace("'", "")
            with st.spinner(f"{model_name} thinking..."):
                response = Complete(
                    model_name,
                    get_cortex_prompt(question, model_name, session),
                )
                st.markdown(response)

        st.session_state.messages.append({"role": "assistant", "content": response})