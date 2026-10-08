-- Minimal schema for the DB-backed integration tests under tests/conversation/
-- (validation e2e, see tests/conversation/test_validation_e2e.py once added).
--
-- Mirrors only the tables this service's own code actually touches or has a
-- real FK dependency on, taken from the `database` repo's Flyway migrations
-- (V1__baseline.sql for auth.user_account/form.task/form.task_form/
-- form.question, V9__line_chat_notify_models.sql for auth.line_identity/
-- chat.conversation/chat.conversation_answer). Copied rather than pulled in
-- live -- `database` is a separate git repo with its own history, same
-- reasoning mobile-backend's testSchemaDDL and web-backend's ci-schema.sql
-- already use. If those migrations change shape, this needs a manual update
-- to match.
--
-- form.section and form.field_validation_rule are deliberately NOT included:
-- this service never queries either directly (ADR 0001's seam) -- form
-- structure and validation rules arrive over HTTP from Kotlin's
-- GET /forms/{formId}, which the tests mock instead of hitting a real DB.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA auth;
CREATE SCHEMA form;
CREATE SCHEMA chat;
CREATE SCHEMA notify;
CREATE SCHEMA agriculture;

CREATE TABLE auth.user_account (
    user_id uuid DEFAULT gen_random_uuid() NOT NULL,
    username character varying,
    password_hash character varying,
    CONSTRAINT pk_user_account PRIMARY KEY (user_id)
);

CREATE TABLE auth.role (
    role_id uuid DEFAULT gen_random_uuid() NOT NULL,
    role_name character varying NOT NULL,
    CONSTRAINT pk_role PRIMARY KEY (role_id)
);

CREATE TABLE auth.user_role (
    user_id uuid NOT NULL,
    role_id uuid NOT NULL,
    CONSTRAINT pk_user_role PRIMARY KEY (user_id, role_id),
    CONSTRAINT fk_user_role_user FOREIGN KEY (user_id) REFERENCES auth.user_account (user_id),
    CONSTRAINT fk_user_role_role FOREIGN KEY (role_id) REFERENCES auth.role (role_id)
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
    close_at timestamp without time zone,
    CONSTRAINT pk_task PRIMARY KEY (task_id)
);

CREATE TABLE form.task_form (
    form_id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    title character varying,
    handler character varying,
    -- V1 baseline column. Nothing read or wrote it until multi-submit;
    -- temp_task_picker's pending-task query now does, so the mirror needs it.
    is_multiple_submit boolean NOT NULL DEFAULT false,
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
    current_page integer NOT NULL DEFAULT 0,
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

-- notify.* -- reminder scheduling + delivery log (ADR 0005/0006). Mirrors
-- src/reminders/models.py; the database repo's Flyway migration is the real
-- source of this shape.
CREATE TABLE notify.reminder_schedule (
    schedule_id uuid NOT NULL DEFAULT gen_random_uuid(),
    task_id uuid NOT NULL,
    cadence character varying NOT NULL,
    time_of_day time without time zone NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_by uuid NOT NULL,
    CONSTRAINT pk_reminder_schedule PRIMARY KEY (schedule_id),
    CONSTRAINT fk_reminder_schedule_task FOREIGN KEY (task_id) REFERENCES form.task (task_id),
    CONSTRAINT fk_reminder_schedule_user FOREIGN KEY (created_by) REFERENCES auth.user_account (user_id)
);

CREATE TABLE notify.reminder_recipient (
    recipient_id uuid NOT NULL DEFAULT gen_random_uuid(),
    schedule_id uuid NOT NULL,
    recipient_type character varying NOT NULL,
    role_id uuid,
    user_id uuid,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    CONSTRAINT pk_reminder_recipient PRIMARY KEY (recipient_id),
    CONSTRAINT fk_reminder_recipient_schedule FOREIGN KEY (schedule_id) REFERENCES notify.reminder_schedule (schedule_id) ON DELETE CASCADE,
    CONSTRAINT fk_reminder_recipient_role FOREIGN KEY (role_id) REFERENCES auth.role (role_id),
    CONSTRAINT fk_reminder_recipient_user FOREIGN KEY (user_id) REFERENCES auth.user_account (user_id),
    CONSTRAINT ck_reminder_recipient_type CHECK (recipient_type IN ('ROLE', 'USER')),
    CONSTRAINT ck_reminder_recipient_target CHECK (
        (recipient_type = 'ROLE' AND role_id IS NOT NULL AND user_id IS NULL)
        OR (recipient_type = 'USER' AND user_id IS NOT NULL AND role_id IS NULL)
    ),
    CONSTRAINT uq_reminder_recipient_role UNIQUE (schedule_id, role_id),
    CONSTRAINT uq_reminder_recipient_user UNIQUE (schedule_id, user_id)
);

CREATE TABLE notify.reminder_log (
    log_id uuid NOT NULL DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    task_id uuid NOT NULL,
    sent_at timestamp without time zone NOT NULL DEFAULT now(),
    channel character varying NOT NULL,
    status character varying NOT NULL,
    CONSTRAINT pk_reminder_log PRIMARY KEY (log_id),
    CONSTRAINT fk_reminder_log_user FOREIGN KEY (user_id) REFERENCES auth.user_account (user_id),
    CONSTRAINT fk_reminder_log_task FOREIGN KEY (task_id) REFERENCES form.task (task_id)
);

-- agriculture.* -- read directly (never written) by src/line/plot_picker.py,
-- the same documented ADR 0001 exception src/line/parent_picker.py takes.
-- Only the three tables and the columns that picker's query actually names;
-- the real tables (database repo V1__baseline.sql) have many more.
CREATE TABLE agriculture.farm (
    farm_id uuid DEFAULT gen_random_uuid() NOT NULL,
    farm_name character varying,
    CONSTRAINT pk_farm PRIMARY KEY (farm_id)
);

CREATE TABLE agriculture.farmer_farm (
    farmer_farm_id uuid DEFAULT gen_random_uuid() NOT NULL,
    farmer_id uuid NOT NULL,
    farm_id uuid NOT NULL,
    CONSTRAINT pk_farmer_farm PRIMARY KEY (farmer_farm_id),
    CONSTRAINT fk_farmer_farm_farm FOREIGN KEY (farm_id) REFERENCES agriculture.farm (farm_id)
);

CREATE TABLE agriculture.plot (
    plot_id uuid DEFAULT gen_random_uuid() NOT NULL,
    farm_id uuid NOT NULL,
    plot_name character varying,
    CONSTRAINT pk_plot PRIMARY KEY (plot_id),
    CONSTRAINT fk_plot_farm FOREIGN KEY (farm_id) REFERENCES agriculture.farm (farm_id)
);
