CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA auth;
CREATE SCHEMA form;
CREATE SCHEMA chat;

CREATE TABLE auth.user_account (
    user_id uuid DEFAULT gen_random_uuid() NOT NULL,
    username character varying,
    password_hash character varying,
    CONSTRAINT pk_user_account PRIMARY KEY (user_id)
);

CREATE TABLE auth.line_identity (
    line_identity_id uuid NOT NULL DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    line_user_id character varying NOT NULL,
    display_name character varying,
    linked_at timestamp without time zone NOT NULL DEFAULT now(),
    CONSTRAINT pk_line_identity PRIMARY KEY (line_identity_id),
    CONSTRAINT uq_line_identity_user UNIQUE (user_id),
    CONSTRAINT uq_line_identity_line_user_id UNIQUE (line_user_id),
    CONSTRAINT fk_line_identity_user FOREIGN KEY (user_id) REFERENCES auth.user_account (user_id)
);

CREATE TABLE form.task (
    task_id uuid DEFAULT gen_random_uuid() NOT NULL,
    title character varying,
    open_at timestamp without time zone,
    CONSTRAINT pk_task PRIMARY KEY (task_id)
);

CREATE TABLE form.task_form (
    form_id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    title character varying,
    handler character varying,
    CONSTRAINT pk_form PRIMARY KEY (form_id),
    CONSTRAINT fk_form_task FOREIGN KEY (task_id) REFERENCES form.task (task_id)
);

CREATE TABLE form.response (
    response_id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_log_id uuid NOT NULL,
    user_id uuid NOT NULL,
    CONSTRAINT pk_response PRIMARY KEY (response_id),
    CONSTRAINT fk_response_user FOREIGN KEY (user_id) REFERENCES auth.user_account (user_id)
);

CREATE TABLE form.question (
    question_id uuid DEFAULT gen_random_uuid() NOT NULL,
    section_id uuid NOT NULL,
    label character varying,
    field_name character varying,
    input_type character varying DEFAULT 'VARCHAR'::character varying,
    sort_order integer,
    is_mandatory boolean DEFAULT false,
    CONSTRAINT pk_question PRIMARY KEY (question_id)
);

CREATE TABLE chat.conversation (
    conversation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    task_id uuid NOT NULL,
    task_form_id uuid NOT NULL,
    status character varying NOT NULL DEFAULT 'active',
    current_question_id uuid,
    parent_answer jsonb,
    CONSTRAINT pk_chat_conversation PRIMARY KEY (conversation_id),
    CONSTRAINT fk_chat_conversation_user FOREIGN KEY (user_id) REFERENCES auth.user_account (user_id),
    CONSTRAINT fk_chat_conversation_task FOREIGN KEY (task_id) REFERENCES form.task (task_id),
    CONSTRAINT fk_chat_conversation_task_form FOREIGN KEY (task_form_id) REFERENCES form.task_form (form_id),
    CONSTRAINT fk_chat_conversation_current_question FOREIGN KEY (current_question_id) REFERENCES form.question (question_id),
    CONSTRAINT ck_chat_conversation_status CHECK (status IN ('active', 'paused', 'completed', 'cancelled'))
);

CREATE TABLE chat.conversation_answer (
    conversation_answer_id uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    question_id uuid NOT NULL,
    answer jsonb,
    source character varying NOT NULL,
    CONSTRAINT pk_chat_conversation_answer PRIMARY KEY (conversation_answer_id),
    CONSTRAINT fk_conversation_answer_conversation FOREIGN KEY (conversation_id) REFERENCES chat.conversation (conversation_id),
    CONSTRAINT fk_conversation_answer_question FOREIGN KEY (question_id) REFERENCES form.question (question_id),
    CONSTRAINT ck_conversation_answer_source CHECK (source IN ('guided_flow', 'llm_extracted'))
);
