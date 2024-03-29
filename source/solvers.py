import random
from abc import ABC
from copy import deepcopy

import numpy as np
from tqdm.notebook import tqdm
import torch
from torch import nn

from deeppavlov import build_model, configs
from deeppavlov.core.common.file import read_json
from deeppavlov.vocabs.wiki_sqlite import WikiSQLiteVocab

from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from .constants import QUESTION_TYPES, DP_RU_BERT_MODEL_PATH, RU_WIKI_PATH
from .utils import Dummy


class BaseSolver(object):
    def __init__(self):
        self.types = QUESTION_TYPES

    def __call__(self, tasks):
        if isinstance(tasks, list):
            answers = []
            for task in tqdm(tasks):
                try:
                    answer = self._solve(task)
                except NotImplementedError:
                    answer = None
                except Exception as e:
                    print(e, task)
                    answer = None
                answers.append(answer)
            return answers
        else:
            return self._solve(tasks)

    def _solve(self, task):
        task_type = self.detect_task_type(task)
        task_solver = self.get_solver_by_type(task_type)
        answer = task_solver(task)
        answer = self.get_answer_representation(task_type, task, answer)
        return answer

    def detect_task_type(self, task):
        return self.types.get(task['type'])

    def get_solver_by_type(self, task_type):
        return getattr(self, '{type}_solver'.format(type=task_type))

    def get_answer_representation(self, task_type, task, answer):
        if task_type == 'multiple_choice':
            corrected_answer = []
            options = task['options']
            for elem in answer:
                if options['number_options']:
                    corrected_answer.append(options['number_options'].index(elem) + 1)
                else:
                    corrected_answer.append(options['letter_options'].index(elem) + 1)
                    corrected_answer[-1] = chr(corrected_answer[-1] + ord('а'))
            answer = ''.join(map(str, corrected_answer))

        if task_type == 'match_terms':
            answer = ''.join(map(str, answer))

        if task_type == 'order':
            corrected_answer = []
            options = task['options']
            for elem in answer:
                corrected_answer.append(elem + 1)
                if not options['number_options']:
                    corrected_answer[-1] = chr(corrected_answer[-1] + ord('а'))
            answer = ''.join(map(str, corrected_answer))

        return answer

    def multiple_choice_solver(self, task):
        raise NotImplementedError('Multiple choice solver is not implemented')

    def question_solver(self, task):
        raise NotImplementedError('Open-ended question solver is not implemented')

    def match_terms_solver(self, task):
        raise NotImplementedError('Match-terms solver is not implemented')

    def percents_solver(self, task):
        raise NotImplementedError('Percents solver is not implemented')

    def order_solver(self, task):
        raise NotImplementedError('Order solver is not implemented')

    def number_solver(self, task):
        raise NotImplementedError('Order solver is not implemented')


class RandomSolver(BaseSolver):
    def multiple_choice_solver(self, task):
        options = task['options']['number_options'] or task['options']['letter_options']
        return random.sample(options, len(options) - 3)

    def question_solver(self, task):
        raise NotImplementedError('Open-ended question cannot be answered randomly')

    def match_terms_solver(self, task):
        options = task['options']
        numbers_amount = len(options['number_options'])
        return [random.randint(1, numbers_amount) for elem in options['letter_options']]

    def percents_solver(self, task):
        return str(random.randint(0, 100))

    def order_solver(self, task):
        options = task['options']
        numbers_amount = len(options['number_options'] or options['letter_options'])
        answer = list(range(numbers_amount))
        random.shuffle(answer)
        return answer

    def number_solver(self, task):
        return str(random.randint(0, 1000))


class SimpleBertSolver(BaseSolver, ABC):
    def __init__(self, model_name, options={}):
        super(SimpleBertSolver, self).__init__()
        self.model = SentenceTransformer(model_name)
        self.options = options

    def encode(self, texts, **kwargs):
        return self.model.encode(texts)

    def multiple_choice_solver(self, task):
        options = task['options']['number_options'] or task['options']['letter_options']
        question = [task['question']]
        question_embedding = self.encode(question, **self.options.get('question_kwargs', {}))[0].reshape(1, -1)
        options_embedding = np.vstack(self.encode(options, **self.options.get('options_kwargs', {})))
        similarity = cosine_similarity(question_embedding, options_embedding)
        answer = np.array(options)[similarity[0].argsort()[-len(options) + 3:]]
        return list(answer)

    def match_terms_solver(self, task):
        number_options = task['options']['number_options']
        letter_options = task['options']['letter_options']
        number_options_embedding = np.vstack(self.encode(number_options, **self.options.get('question_kwargs', {})))
        letter_options_embedding = np.vstack(self.encode(letter_options, **self.options.get('options_kwargs', {})))
        similarity = cosine_similarity(letter_options_embedding, number_options_embedding)
        return list(similarity.argmax(1) + 1)


class SimpleDPBertSolver(SimpleBertSolver, ABC):
    def __init__(self, config=configs.embedder.bert_embedder, emb_type='sent_mean_embs', options={}):
        self.types = QUESTION_TYPES
        bert_config = read_json(config)
        bert_config['metadata']['variables']['BERT_PATH'] = DP_RU_BERT_MODEL_PATH
        self.model = build_model(bert_config)
        self.emb_type = emb_type
        self.options = options

    def encode(self, texts, emb_type=None):
        tokens, token_embs, subtokens, subtoken_embs, sent_max_embs, sent_mean_embs, bert_pooler_outputs = self.model(
            texts)
        return locals()[emb_type or self.emb_type]


class ClassificationSolver(BaseSolver, ABC):
    def __init__(self, embedder, reduction_method, classifier, emb_type='sent_mean_embs'):
        super().__init__()
        self.embedder = embedder
        self.reduction_method = reduction_method
        self.classifier = classifier
        self.emb_type = emb_type

    def question_process_task(self, task):
        raise NotImplementedError()

    def percents_process_task(self, task):
        raise NotImplementedError()

    def order_process_task(self, task):
        raise NotImplementedError()

    def number_process_task(self, task):
        raise NotImplementedError()

    def multiple_choice_process_task(self, task):
        options = deepcopy(task['options'])
        question = self.encode([task['question']])
        letter_options = self.encode(options['letter_options']) if options['letter_options'] else []
        number_options = self.encode(options['number_options']) if options['number_options'] else []
        answer = task['answers'][0]
        X = []
        y = []
        if task['options']['letter_options']:
            for i, letter_opt in enumerate(letter_options):
                letter = chr(ord('а') + i)
                X.append(np.concatenate([question[0], letter_opt], axis=-1))
                y.append(letter in answer)
        else:
            for i, number_opt in enumerate(number_options):
                X.append(np.concatenate([question[0], number_opt], axis=-1))
                y.append(str(i + 1) in answer)
        return X, y

    def match_terms_process_task(self, task):
        options = deepcopy(task['options'])
        try:
            letter_options = self.encode(options['letter_options'])
            number_options = self.encode(options['number_options'])
        except:
            raise NotImplementedError()
        answer = task['answers'][0]
        X = []
        y = []
        for i, number_opt in enumerate(number_options):
            for j, letter_opt in enumerate(letter_options):
                X.append(np.concatenate([letter_opt, number_opt], axis=-1))
                y.append(answer[j] == str(i + 1))
        return X, y

    def get_processor_by_type(self, task_type):
        return getattr(self, '{type}_process_task'.format(type=task_type))

    def process_task(self, task):
        task_type = self.detect_task_type(task)
        return self.get_processor_by_type(task_type)(task)

    # tasks -> list[(question, answer)], correct_answer: list[bool]
    def process_tasks(self, tasks):
        X = []
        y = []
        for task in tqdm(tasks):
            try:
                task_X, task_y = self.process_task(task)
                X.extend(task_X)
                y.extend(task_y)
            except NotImplementedError:
                pass
            except Exception as e:
                print(e, task)
        return X, y

    def encode(self, texts, emb_type=None):
        tokens, token_embs, subtokens, subtoken_embs, sent_max_embs, sent_mean_embs, bert_pooler_outputs = self.embedder(
            texts)
        return locals()[emb_type or self.emb_type]

    def reduce_dim(self, X):
        return self.reduction_method.transform(X)

    def train(self, tasks):
        print('processing and encoding tasks...  ', end='')
        X, y = self.process_tasks(tasks)
        print('fitting reduction method....  ', end='')
        X = np.array(X)
        self.reduction_method.fit(X)
        print('reducing dimensions.....  ', end='')
        X = self.reduce_dim(X)
        print('fitting classifier......   ', end='')
        self.classifier.fit(X, y)

    def multiple_choice_solver(self, task):
        options = task['options']['number_options'] or task['options']['letter_options']
        pred = self.predict(task)
        answer = np.array(options)[pred.argsort()[-len(options) + 3:]]
        return list(answer)

    def match_terms_solver(self, task):
        number_options = task['options']['number_options']
        letter_options = task['options']['letter_options']
        pred = self.predict(task).reshape((len(number_options), len(letter_options))).T
        return list(pred.argmax(1) + 1)

    def predict(self, task):
        X, y = self.process_task(task)
        X = self.reduce_dim(X)
        try:
            return self.classifier.predict_proba(X)[:, 0]
        except AttributeError:
            try:
                return self.classifier.decision_function(X)[:, 0]
            except AttributeError:
                return self.classifier.predict(X)


class AnotherBERTClassificationSolver(ClassificationSolver, ABC):
    def encode(self, texts, emb_type=None):
        return self.embedder.encode(texts)


class ContextBertSolver(SimpleBertSolver, ABC):
    def __init__(self, model_name, options={}, download=False):
        super(ContextBertSolver, self).__init__(model_name, options)
        self.ranker = build_model(configs.doc_retrieval.ru_ranker_tfidf_wiki, download=download)
        self.ruwiki = WikiSQLiteVocab(RU_WIKI_PATH)
        self.retriever = build_model(configs.squad.squad_ru_rubert, download=download)

    def get_context(self, question):
        context = self.ruwiki(self.ranker(question))
        context = list(map(lambda x: x.replace('\n', ' ').replace('\xa0', ' ').replace('\xad', ''), context))
        return context

    def retrieve_answer(self, context, question):
        return self.retriever(context, question)

    def match_terms_solver(self, task):
        raise NotImplementedError

    def multiple_choice_solver(self, task):
        options = task['options']['number_options'] or task['options']['letter_options']
        question = [task['question']]
        context = self.get_context(question + options)
        retrieved_answer = self.retrieve_answer(context, question)[0]
        if retrieved_answer[0] == '':
            retrieved_answer = question
        retrieved_answer_embedding = self.encode(retrieved_answer)[0].reshape(1, -1)
        options_embedding = np.vstack(self.encode(options))
        similarity = cosine_similarity(retrieved_answer_embedding, options_embedding)
        answer = np.array(options)[similarity[0].argsort()[-len(options) + 3:]]
        return list(answer)


class OptionsContextBertSolver(ContextBertSolver):

    def multiple_choice_solver(self, task):
        options = task['options']['number_options'] or task['options']['letter_options']
        question = [task['question']]
        question_context = self.get_context(question)
        question_context_embedding = self.encode(question_context)[0].reshape(1, -1)
        options_embedding = np.vstack(self.encode(list(map(lambda x: self.get_context([x])[0], options))))
        similarity = cosine_similarity(question_context_embedding, options_embedding)
        answer = np.array(options)[similarity[0].argsort()[-len(options) + 3:]]
        return list(answer)


class QuadraticForm(nn.Module):
    def __init__(self, input_size, projection_space=None, activation=nn.Identity):
        super(QuadraticForm, self).__init__()
        if projection_space:
            self.answer_projection = nn.Sequential(
                nn.Linear(input_size, projection_space),
                activation()
            )
            self.question_projection = nn.Sequential(
                nn.Linear(input_size, projection_space),
                activation()
            )
            self.need_projection = True
        else:
            projection_space = input_size
            self.need_projection = False
        self.operator = nn.Linear(projection_space, projection_space)

    def forward(self, question, answer):
        if self.need_projection:
            question = self.question_projection(question)
            answer = self.answer_projection(answer)
        return torch.einsum('bs,bs->b', self.operator(question), answer)


class QuadraticFormSolver(ClassificationSolver, ABC):
    def __init__(self, embedder, emb_dims):
        super(QuadraticFormSolver, self).__init__(embedder, Dummy, Dummy)
        self.embedder = embedder
        self.emb_dims = emb_dims
        self.model = QuadraticForm(emb_dims)
        self.opt = torch.optim.SGD(self.model.parameters(), lr=0.001)

    def train(self, tasks):
        print('processing and encoding tasks...  ', end='')
        X, Y = self.process_tasks(tasks)
        print('training...  ')
        loss_func = nn.BCEWithLogitsLoss()
        for i in range(2):
            for start in range(0, len(X), 64):
                end = min(start + 64, len(X))
                x = np.array(X[start:end])
                y = np.array(Y[start:end])
                self.opt.zero_grad()
                q = torch.from_numpy(x[:, :self.emb_dims])
                a = torch.from_numpy(x[:, self.emb_dims:])
                pred = self.model(q, a)
                loss = loss_func(pred, torch.from_numpy(y.astype(float)))
                loss.backward()
                self.opt.step()
                print('loss:', loss.item())

    def predict(self, task):
        X, y = self.process_task(task)
        X = np.array(X)
        q, a = X[:, :self.emb_dims], X[:, self.emb_dims:]
        q = torch.from_numpy(q)
        a = torch.from_numpy(a)
        with torch.no_grad():
            pred = self.model(q, a)
        return pred.numpy()


class QuadraticFormWithProjectionsSolver(QuadraticFormSolver, ABC):
    def __init__(self, embedder, emb_dims, projection_space):
        super(QuadraticFormSolver, self).__init__(embedder, Dummy, Dummy)
        self.embedder = embedder
        self.emb_dims = emb_dims
        self.model = QuadraticForm(emb_dims, projection_space=projection_space, activation=nn.Tanh)
        self.opt = torch.optim.Adam(self.model.parameters(), lr=0.0001)
