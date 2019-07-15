import json
from sqlnet.lib.dbengine import DBEngine
import numpy as np
from tqdm import tqdm
from sqlnet.model.sqlbert import SQLBert
import torch

def pos_in_tokens(target_str, tokens):
	max_len = 0
	s, e = -1, -1
	for i in range(len(tokens)):
		if not target_str.startswith(tokens[i]):
			continue
		curlen = len(tokens[i])
		if curlen > max_len:
			max_len = curlen
			s, e = i, i + 1
		for j in range(i+1, len(tokens)):
			if target_str[curlen:].startswith(tokens[j]):
				curlen += len(tokens[j])
				if curlen > max_len:
					max_len = curlen
					s, e = i, j + 1
			else: break

			if curlen >= len(target_str):
				return i, j+1
	return s, e

#
# def most_similar(s, slist):
#     """从词表中找最相近的词（当无法全匹配的时候）
#     """
#     if len(slist) == 0:
#         return s
#     scores = [editdistance.eval(s, t) for t in slist]
#     return slist[np.argmin(scores)]
#
#
# def most_similar_2(w, s):
#     """从句子s中找与w最相近的片段，
#     借助分词工具和ngram的方式尽量精确地确定边界。
#     """
#     sw = jieba.lcut(s)
#     sl = list(sw)
#     sl.extend([''.join(i) for i in zip(sw, sw[1:])])
#     sl.extend([''.join(i) for i in zip(sw, sw[1:], sw[2:])])
#     return most_similar(w, sl)

def load_data(sql_paths, table_paths, use_small=False):
	if not isinstance(sql_paths, list):
		sql_paths = (sql_paths,)
	if not isinstance(table_paths, list):
		table_paths = (table_paths,)
	sql_data = []
	table_data = {}

	for SQL_PATH in sql_paths:
		with open(SQL_PATH, encoding='utf-8') as inf:
			for idx, line in enumerate(inf):
				sql = json.loads(line.strip())
				if use_small and idx >= 1000:
					break
				sql_data.append(sql)
		print("Loaded %d data from %s" % (len(sql_data), SQL_PATH))

	for TABLE_PATH in table_paths:
		with open(TABLE_PATH, encoding='utf-8') as inf:
			for line in inf:
				tab = json.loads(line.strip())
				table_data[tab[u'id']] = tab
		print("Loaded %d data from %s" % (len(table_data), TABLE_PATH))

	ret_sql_data = []
	for sql in sql_data:
		if sql[u'table_id'] in table_data:
			ret_sql_data.append(sql)

	return ret_sql_data, table_data


def load_dataset(toy=False, use_small=False, mode='train'):
	print("Loading dataset")
	dev_sql, dev_table = load_data('data/val/val.json', 'data/val/val.tables.json', use_small=use_small)
	dev_db = 'data/val/val.db'
	if mode == 'train':
		train_sql, train_table = load_data('data/train/train.json', 'data/train/train.tables.json', use_small=use_small)
		train_db = 'data/train/train.db'
		return train_sql, train_table, train_db, dev_sql, dev_table, dev_db
	elif mode == 'test':
		test_sql, test_table = load_data('data/test/test.json', 'data/test/test.tables.json', use_small=use_small)
		test_db = 'data/test/test.db'
		return dev_sql, dev_table, dev_db, test_sql, test_table, test_db


def to_batch_seq(sql_data, table_data, idxes, st, ed, tokenizer=None, ret_vis_data=False):
	q_seq = []
	col_seq = []
	col_num = []
	ans_seq = []
	gt_cond_seq = []
	vis_seq = []
	sel_num_seq = []
	header_type = []
	for i in range(st, ed):
		sql = sql_data[idxes[i]]
		sel_num = len(sql['sql']['sel'])
		sel_num_seq.append(sel_num)
		conds_num = len(sql['sql']['conds'])
		if tokenizer:
			q = tokenizer.tokenize(sql['question'])
			col = [tokenizer.tokenize(header) for header in table_data[sql['table_id']]['header']]

		else:
			q = [char for char in sql['question']]
			col = [[char for char in header] for header in table_data[sql['table_id']]['header']]
		q_seq.append(q)
		col_seq.append(col)
		col_num.append(len(table_data[sql['table_id']]['header']))
		ans_seq.append(
			(
				len(sql['sql']['agg']),
				sql['sql']['sel'],
				sql['sql']['agg'],
				conds_num,
				tuple(x[0] for x in sql['sql']['conds']),
				tuple(x[1] for x in sql['sql']['conds']),
				sql['sql']['cond_conn_op'],
			))
		gt_cond_seq.append(sql['sql']['conds'])
		vis_seq.append((sql['question'], table_data[sql['table_id']]['header']))
		header_type.append(table_data[sql['table_id']]['types'])
	# q_seq: char-based sequence of question
	# gt_sel_num: number of selected columns and aggregation functions
	# col_seq: char-based column name
	# col_num: number of headers in one table
	# ans_seq: (sel, number of conds, sel list in conds, op list in conds)
	# gt_cond_seq: ground truth of conds
	if ret_vis_data:
		return q_seq, sel_num_seq, col_seq, col_num, ans_seq, gt_cond_seq, header_type, vis_seq
	else:
		return q_seq, sel_num_seq, col_seq, col_num, ans_seq, gt_cond_seq, header_type


def pad_batch_seqs(seqs, pad=None, max_len=None):
	if not max_len:
		max_len = max([len(s) for s in seqs])
	if not pad:
		pad = 0
	for i in range(len(seqs)):
		if len(seqs[i]) > max_len:
			seqs[i] = seqs[i][:max_len]
		else:
			seqs[i].extend([pad] * (max_len - len(seqs[i])))

	return seqs


def gen_batch_bert_seq(tokenizer, q_seq, col_seq, header_type, max_len=200):
	input_seq = []  # 输入编号
	q_mask = []  # NL mask
	col_mask = []  # columns mask
	col_index = [] # columns starting index
	token_type_ids = []  # sentence A/B
	attention_mask = []  # length mask

	q_lens = []
	col_nums = []

	batch_size = len(q_seq)
	for i in range(batch_size):
		text_a = ['[CLS]'] + q_seq[i] + ['[SEP]']
		text_b = []
		for col_idx, col in enumerate(col_seq[i]):
			type_token = '[unused1]' if header_type[i][col_idx] == 'text' else '[unused2]'
			text_b.append(type_token)
			text_b.extend(col)
			text_b.append('[SEP]')

		overflow = len(text_a) + len(text_b) - max_len
		if overflow > 0:
			if overflow + 1 < len(text_a):
				text_a = text_a[:len(text_a)-overflow-1] + ['[SEP]']
			else:
				text_b = text_b[:len(text_b)-overflow-1] + ['[SEP]']
		inp_seq = text_a + text_b
		input_seq.append(inp_seq)
		q_mask.append([1]*(len(text_a) - 2))
		q_lens.append(len(text_a) - 2)
		token_type_ids.append([0]*len(text_a) + [1]*len(text_b))
		attention_mask.append([1]*len(inp_seq))

		col_idx = []
		for i in range(len(text_a)-1, len(inp_seq)):
			# if inp_seq[i] == '[CLS]':
			# 	col_idx.append(i)
			if inp_seq[i] == '[SEP]':
				col_idx.append(i)
		col_mask.append([1]*(len(col_idx)-1))
		col_nums.append((len(col_idx)-1))
		col_index.append(col_idx)

	input_seq = pad_batch_seqs(input_seq, '[PAD]')
	input_seq = [tokenizer.convert_tokens_to_ids(sq) for sq in input_seq]
	q_mask = pad_batch_seqs(q_mask)
	col_mask = pad_batch_seqs(col_mask)
	col_index = pad_batch_seqs(col_index)
	token_type_ids = pad_batch_seqs(token_type_ids)
	attention_mask = pad_batch_seqs(attention_mask)

	return (input_seq, q_mask, col_mask, col_index, token_type_ids, attention_mask), q_lens, col_nums


def to_batch_seq_test(sql_data, table_data, idxes, st, ed, tokenizer=None):
	q_seq = []
	col_seq = []
	col_num = []
	raw_seq = []
	table_ids = []
	header_type = []
	for i in range(st, ed):
		sql = sql_data[idxes[i]]

		if tokenizer:
			q = tokenizer.tokenize(sql['question'])
			col = [tokenizer.tokenize(header) for header in table_data[sql['table_id']]['header']]
		else:
			q = [char for char in sql['question']]
			col = [[char for char in header] for header in table_data[sql['table_id']]['header']]
		q_seq.append(q)
		col_seq.append(col)
		col_num.append(len(table_data[sql['table_id']]['header']))
		raw_seq.append(sql['question'])
		table_ids.append(sql_data[idxes[i]]['table_id'])
		header_type.append(table_data[sql['table_id']]['types'])
	return q_seq, col_seq, col_num, raw_seq, table_ids, header_type


def generate_gt_where_seq_test(q, gt_cond_seq):
	ret_seq = []
	for cur_q, ans in zip(q, gt_cond_seq):
		temp_q = u"".join(cur_q)
		cur_q = [u'<BEG>'] + cur_q + [u'<END>']
		record = []
		record_cond = []
		for cond in ans:
			if cond[2] not in temp_q:
				record.append((False, cond[2]))
			else:
				record.append((True, cond[2]))
		for idx, item in enumerate(record):
			temp_ret_seq = []
			if item[0]:
				temp_ret_seq.append(0)
				temp_ret_seq.extend(list(range(temp_q.index(item[1]) + 1, temp_q.index(item[1]) + len(item[1]) + 1)))
				temp_ret_seq.append(len(cur_q) - 1)
			else:
				temp_ret_seq.append([0, len(cur_q) - 1])
			record_cond.append(temp_ret_seq)
		ret_seq.append(record_cond)
	return ret_seq


def gen_bert_labels(q_seq, q_lens, col_nums, ans_seq, gt_cond_seq):


	q_max_len = max(q_lens)
	col_max_len = max(col_nums)

	# labels init
	where_conn_label = np.array([x[6] for x in ans_seq])  # (None, )
	sel_num_label = np.array([0 for _ in ans_seq])  # (None, )
	where_num_label = np.array([0 for _ in ans_seq]) # (None, )
	sel_col_label = np.array([[0] * col_max_len for _ in ans_seq], dtype=np.float)  # (None, col_max_len)
	sel_agg_label = np.array([[-1] * col_max_len for _ in ans_seq])  # (None, col_max_len)
	where_col_label = np.array([[0] * col_max_len for _ in ans_seq], dtype=np.float)  # (None, col_max_len)
	where_op_label = np.array([[-1] * col_max_len for _ in ans_seq]) # (None, col_max_len)

	where_start_label = np.array([[-1] * col_max_len for _ in ans_seq])
	where_end_label = np.array([[-1] * col_max_len for _ in ans_seq])

	for b in range(len(gt_cond_seq)):
		num_conds = len(gt_cond_seq[b])
		if num_conds == 0:
			where_col_label[b] = 1.0 / col_nums[b] # 分散
			mass = 0
		else:
			mass = 1 / num_conds

		for cond in gt_cond_seq[b]:
			if cond[0] >= col_nums[b]:
				continue
			s, e = pos_in_tokens(cond[2], q_seq[b])
			if s >= 0:
				s = min(s, q_lens[b] - 1)
				e = min(e - 1, q_lens[b] - 1)
				where_op_label[b][cond[0]] = cond[1]
				where_col_label[b][cond[0]] += mass
				where_start_label[b][cond[0]] = s
				where_end_label[b][cond[0]] = e
		if num_conds > 0:
			where_num_label[b] = (where_col_label[b] > 0).sum()

		for b in range(len(ans_seq)):
			sel = ans_seq[b][1]
			agg = ans_seq[b][2]
			sel_num_label[b] = len(sel)
			mass = 1 / sel_num_label[b]
			sel_col_label[b][sel] = mass
			sel_agg_label[b][sel] = agg

	return where_conn_label, sel_num_label, where_num_label, sel_col_label, sel_agg_label, \
		   where_col_label, where_op_label, where_start_label, where_end_label

def to_batch_query(sql_data, idxes, st, ed):
	query_gt = []
	table_ids = []
	for i in range(st, ed):
		sql_data[idxes[i]]['sql']['conds'] = sql_data[idxes[i]]['sql']['conds']
		query_gt.append(sql_data[idxes[i]]['sql'])
		table_ids.append(sql_data[idxes[i]]['table_id'])
	return query_gt, table_ids


def epoch_train(model, optimizer, batch_size, sql_data, table_data, tokenizer=None):
	model.train()
	perm = np.random.permutation(len(sql_data))
	cum_loss = 0.0
	for st in tqdm(range(len(sql_data) // batch_size + 1)):
		if st * batch_size == len(perm):
			break
		ed = (st + 1) * batch_size if (st + 1) * batch_size < len(perm) else len(perm)
		st = st * batch_size
		if isinstance(model, SQLBert):
			# bert training
			q_seq, gt_sel_num, col_seq, col_num, ans_seq, gt_cond_seq, header_type = to_batch_seq(sql_data, table_data, perm, st, ed,
																					 tokenizer=tokenizer)

			bert_inputs, q_lens, col_nums = gen_batch_bert_seq(tokenizer, q_seq, col_seq, header_type)
			logits = model.forward(bert_inputs)  # condconn_logits, condop_logits, sel_agg_logits, q2col_logits

			# gen label
			labels = gen_bert_labels(q_seq, q_lens, col_nums, ans_seq, gt_cond_seq)

			# compute loss
			loss = model.loss(logits, labels, q_lens, col_nums)
		else:

			q_seq, gt_sel_num, col_seq, col_num, ans_seq, gt_cond_seq, header_type = to_batch_seq(sql_data, table_data, perm, st, ed)
			# q_seq: char-based sequence of question
			# gt_sel_num: number of selected columns and aggregation functions
			# col_seq: char-based column name
			# col_num: number of headers in one table
			# ans_seq: (sel, number of conds, sel list in conds, op list in conds)
			# gt_cond_seq: ground truth of conds
			gt_where_seq = generate_gt_where_seq_test(q_seq, gt_cond_seq)
			gt_sel_seq = [x[1] for x in ans_seq]
			score = model.forward(q_seq, col_seq, col_num, gt_where=gt_where_seq, gt_cond=gt_cond_seq, gt_sel=gt_sel_seq,
								  gt_sel_num=gt_sel_num)
			# sel_num_score, sel_col_score, sel_agg_score, cond_score, cond_rela_score

			# compute loss
			loss = model.loss(score, ans_seq, gt_where_seq)

		optimizer.zero_grad()
		loss.backward()
		optimizer.step()
		cum_loss += loss.data.cpu().numpy() * (ed - st)
	return cum_loss / len(sql_data)



def predict_test(model, batch_size, sql_data, table_data, output_path, tokenizer=None):
	model.eval()
	perm = list(range(len(sql_data)))
	fw = open(output_path, 'w')
	for st in tqdm(range(len(sql_data) // batch_size + 1)):
		if st * batch_size == len(perm):
			break
		ed = (st + 1) * batch_size if (st + 1) * batch_size < len(perm) else len(perm)
		st = st * batch_size
		with torch.no_grad():
			if isinstance(model, SQLBert):
				q_seq, col_seq, col_num, raw_q_seq, table_ids, header_type= to_batch_seq_test(sql_data, table_data, perm, st, ed,
																				  tokenizer=tokenizer)
				bert_inputs, q_lens, col_nums = gen_batch_bert_seq(tokenizer, q_seq, col_seq, header_type)
				score = model.forward(bert_inputs, return_logits=False)
			else:
				q_seq, col_seq, col_num, raw_q_seq, table_ids, header_type = to_batch_seq_test(sql_data, table_data, perm, st, ed)
				score = model.forward(q_seq, col_seq, col_num)
			sql_preds = model.gen_query(score, q_seq, col_seq, raw_q_seq)
		for sql_pred in sql_preds:
			sql_pred = eval(str(sql_pred))
			fw.writelines(json.dumps(sql_pred, ensure_ascii=False) + '\n')
			# fw.writelines(json.dumps(sql_pred,ensure_ascii=False).encode('utf-8')+'\n')
	fw.close()



def epoch_acc(model, batch_size, sql_data, table_data, db_path, tokenizer=None):
	engine = DBEngine(db_path)
	model.eval()
	perm = list(range(len(sql_data)))
	badcase = 0
	one_acc_num, tot_acc_num, ex_acc_num = 0.0, 0.0, 0.0
	for st in tqdm(range(len(sql_data) // batch_size + 1)):
		ed = (st + 1) * batch_size if (st + 1) * batch_size < len(perm) else len(perm)
		st = st * batch_size

		q_seq, gt_sel_num, col_seq, col_num, ans_seq, gt_cond_seq, header_type, raw_data = \
			to_batch_seq(sql_data, table_data, perm, st, ed, tokenizer=tokenizer, ret_vis_data=True)
		query_gt, table_ids = to_batch_query(sql_data, perm, st, ed)
		# query_gt: ground truth of sql, data['sql'], containing sel, agg, conds:{sel, op, value}
		raw_q_seq = [x[0] for x in raw_data]  # original question

		# try:
		with torch.no_grad():
			if isinstance(model, SQLBert):
				bert_inputs, q_lens, col_nums = gen_batch_bert_seq(tokenizer, q_seq, col_seq, header_type)
				score = model.forward(bert_inputs, return_logits=False)
			else:
				score = model.forward(q_seq, col_seq, col_num)
		# generate predicted format
		pred_queries = model.gen_query(score, q_seq, col_seq, raw_q_seq)
		one_err, tot_err = check_acc(raw_data, pred_queries, query_gt)
		# except:
		# 	badcase += 1
		# 	print('badcase', badcase)
		# 	continue
		one_acc_num += (ed - st - one_err)
		tot_acc_num += (ed - st - tot_err)

		# Execution Accuracy
		for sql_gt, sql_pred, tid in zip(query_gt, pred_queries, table_ids):
			ret_gt = engine.execute(tid, sql_gt['sel'], sql_gt['agg'], sql_gt['conds'], sql_gt['cond_conn_op'])
			try:
				ret_pred = engine.execute(tid, sql_pred['sel'], sql_pred['agg'], sql_pred['conds'],
										  sql_pred['cond_conn_op'])
			except:
				ret_pred = None
			ex_acc_num += (ret_gt == ret_pred)
	return one_acc_num / len(sql_data), tot_acc_num / len(sql_data), ex_acc_num / len(sql_data)


def check_acc(vis_info, pred_queries, gt_queries):
	def gen_cond_str(conds, header):
		COND_OPS = ['>', '<', '==', '!=']
		if len(conds) == 0:
			return 'None'
		cond_str = []
		for cond in conds:
			cond_str.append(header[cond[0]] + ' ' +
							COND_OPS[cond[1]] + ' ' + cond[2].lower())
		return 'WHERE ' + ' AND '.join(cond_str)

	tot_err = sel_num_err = agg_err = sel_err = 0.0
	cond_num_err = cond_col_err = cond_op_err = cond_val_err = cond_rela_err = 0.0
	for b, (pred_qry, gt_qry) in enumerate(zip(pred_queries, gt_queries)):
		good = True
		sel_pred, agg_pred, where_rela_pred = pred_qry['sel'], pred_qry['agg'], pred_qry['cond_conn_op']
		sel_gt, agg_gt, where_rela_gt = gt_qry['sel'], gt_qry['agg'], gt_qry['cond_conn_op']

		if where_rela_gt != where_rela_pred:
			good = False
			cond_rela_err += 1

		if len(sel_pred) != len(sel_gt):
			good = False
			sel_num_err += 1

		pred_sel_dict = {k: v for k, v in zip(list(sel_pred), list(agg_pred))}
		gt_sel_dict = {k: v for k, v in zip(sel_gt, agg_gt)}
		if set(sel_pred) != set(sel_gt):
			good = False
			sel_err += 1
		agg_pred = [pred_sel_dict[x] for x in sorted(pred_sel_dict.keys())]
		agg_gt = [gt_sel_dict[x] for x in sorted(gt_sel_dict.keys())]
		if agg_pred != agg_gt:
			good = False
			agg_err += 1

		cond_pred = pred_qry['conds']
		cond_gt = gt_qry['conds']
		if len(cond_pred) != len(cond_gt):
			good = False
			cond_num_err += 1
		else:
			cond_op_pred, cond_op_gt = {}, {}
			cond_val_pred, cond_val_gt = {}, {}
			for p, g in zip(cond_pred, cond_gt):
				cond_op_pred[p[0]] = p[1]
				cond_val_pred[p[0]] = p[2]
				cond_op_gt[g[0]] = g[1]
				cond_val_gt[g[0]] = g[2]

			if set(cond_op_pred.keys()) != set(cond_op_gt.keys()):
				cond_col_err += 1
				good = False

			where_op_pred = [cond_op_pred[x] for x in sorted(cond_op_pred.keys())]
			where_op_gt = [cond_op_gt[x] for x in sorted(cond_op_gt.keys())]
			if where_op_pred != where_op_gt:
				cond_op_err += 1
				good = False

			where_val_pred = [cond_val_pred[x] for x in sorted(cond_val_pred.keys())]
			where_val_gt = [cond_val_gt[x] for x in sorted(cond_val_gt.keys())]
			if where_val_pred != where_val_gt:
				cond_val_err += 1
				good = False

		if not good:
			tot_err += 1

	return np.array((sel_num_err, sel_err, agg_err, cond_num_err, cond_col_err, cond_op_err, cond_val_err,
					 cond_rela_err)), tot_err

def load_word_emb(file_name):
	print('Loading word embedding from %s' % file_name)
	f = open(file_name)
	ret = json.load(f)
	f.close()
	# ret = {}
	# with open(file_name, encoding='latin') as inf:
	#     ret = json.load(inf)
	#     for idx, line in enumerate(inf):
	#         info = line.strip().split(' ')
	#         if info[0].lower() not in ret:
	#             ret[info[0]] = np.array([float(x) for x in info[1:]])
	return ret