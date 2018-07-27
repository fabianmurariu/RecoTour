import numpy as np
import pandas as pd
import random
import os
import xgboost as xgb
import lightgbm as lgb
import catboost as ctb
import warnings
import multiprocessing

from joblib import Parallel, delayed
from recutils.average_precision import mapk
from functools import reduce
from hyperopt import hp, tpe, fmin, Trials

warnings.filterwarnings("ignore")
cores = multiprocessing.cpu_count()


def lgb_objective(params):
	"""
	objective function for lightgbm.
	"""

	# hyperopt casts as float
	params['num_boost_round'] = int(params['num_boost_round'])
	params['num_leaves'] = int(params['num_leaves'])

	# need to be passed as parameter
	params['verbose'] = -1
	params['seed'] = 1

	cv_result = lgb.cv(
		params,
		lgtrain,
		nfold=3,
		metrics='rmse',
		num_boost_round=params['num_boost_round'],
		early_stopping_rounds=20,
		stratified=False,
		)

	early_stop_dict[lgb_objective.i] = len(cv_result['rmse-mean'])
	error = cv_result['rmse-mean'][-1]
	print("INFO: iteration {} error {:.3f}".format(lgb_objective.i, error))

	lgb_objective.i+=1

	return error


def lgb_objective_map(params):
	"""
	objective function for lightgbm.
	"""

	# hyperopt casts as float
	params['num_boost_round'] = int(params['num_boost_round'])
	params['num_leaves'] = int(params['num_leaves'])

	# need to be passed as parameter
	params['verbose'] = -1
	params['seed'] = 1

	cv_result = lgb.cv(
	params,
	lgtrain,
	nfold=3,
	metrics='rmse',
	num_boost_round=params['num_boost_round'],
	early_stopping_rounds=20,
	stratified=False,
	)
	early_stop_dict[lgb_objective_map.i] = len(cv_result['rmse-mean'])
	params['num_boost_round'] = len(cv_result['rmse-mean'])

	model = lgb.LGBMRegressor(**params)
	model.fit(train,y_train,feature_name=all_cols,categorical_feature=cat_cols)
	preds = model.predict(X_valid)

	df_eval['interest'] = preds
	df_ranked = df_eval.sort_values(['user_id_hash', 'interest'], ascending=[False, False])
	df_ranked = (df_ranked
		.groupby('user_id_hash')['coupon_id_hash']
		.apply(list)
		.reset_index())
	recomendations_dict = pd.Series(df_ranked.coupon_id_hash.values,
		index=df_ranked.user_id_hash).to_dict()

	actual = []
	pred = []
	for k,_ in recomendations_dict.items():
		actual.append(list(interactions_valid_dict[k]))
		pred.append(list(recomendations_dict[k]))

	result = mapk(actual,pred)
	print("INFO: iteration {} MAP {:.3f}".format(lgb_objective_map.i, result))

	lgb_objective_map.i+=1

	return 1-result

# This approach will be perhaps the one easier to understand. We have user
# features, item features and a target (interest), so let's turn this into a
# supervised problem and fit a regressor. Since this is a "standard" technique
# I will use this opportunity to illustrate a variety of tools around ML in
# general and boosted methods in particular
inp_dir = "../datasets/Ponpare/data_processed/"
train_dir = "train"
valid_dir = "valid"

# train coupon features
df_coupons_train_feat = pd.read_pickle(os.path.join(inp_dir, train_dir, 'df_coupons_train_feat.p'))

# In general, when using boosted methods, the presence of correlated or
# redundant features is not a big deal, since these will be ignored through
# the boosting rounds. However, for clarity and to reduce the chances of
# overfitting, we will select a subset of features here. All the numerical
# features have corresponding categorical ones, so we will keep those in
# moving forward. In addition, if we remember, for valid period, validend and
# validfrom, we used to methods to inpute NaN. Method1: Considering NaN as
# another category and Method2: replace NaN first in the object/numeric column
# and then turning the column into categorical. To start with, we will use
# Method1 here.
drop_cols = [c for c in df_coupons_train_feat.columns
    if ((not c.endswith('_cat')) or ('method2' in c)) and (c!='coupon_id_hash')]
df_coupons_train_cat_feat = df_coupons_train_feat.drop(drop_cols, axis=1)

# train user features: there are a lot of features for users, both, numerical
# and categorical. We keep them all
df_users_train_feat = pd.read_pickle(os.path.join(inp_dir, train_dir, 'df_user_train_feat.p'))

# interest dataframe
df_interest = pd.read_pickle(os.path.join(inp_dir, train_dir, 'df_interest.p'))

df_train = pd.merge(df_interest, df_users_train_feat, on='user_id_hash')
df_train = pd.merge(df_train, df_coupons_train_cat_feat, on = 'coupon_id_hash')

# for the time being we ignore recency
df_train.drop(['user_id_hash','coupon_id_hash','recency_factor'], axis=1, inplace=True)
train = df_train.drop('interest', axis=1)
y_train = df_train.interest
all_cols = train.columns.tolist()
cat_cols = [c for c in train.columns if c.endswith("_cat")]

lgtrain = lgb.Dataset(train,
	label=y_train,
	feature_name=all_cols,
	categorical_feature = cat_cols,
	free_raw_data=False)

# ---------------------------------------------------------------
# validation activities
df_purchases_valid = pd.read_pickle(os.path.join(inp_dir, 'valid', 'df_purchases_valid.p'))
df_visits_valid = pd.read_pickle(os.path.join(inp_dir, 'valid', 'df_visits_valid.p'))
df_visits_valid.rename(index=str, columns={'view_coupon_id_hash': 'coupon_id_hash'}, inplace=True)

# Read the validation coupon features
df_coupons_valid_feat = pd.read_pickle(os.path.join(inp_dir, 'valid', 'df_coupons_valid_feat.p'))
df_coupons_valid_cat_feat = df_coupons_valid_feat.drop(drop_cols, axis=1)

# subset users that were seeing in training
train_users = df_interest.user_id_hash.unique()
df_vva = df_visits_valid[df_visits_valid.user_id_hash.isin(train_users)]
df_pva = df_purchases_valid[df_purchases_valid.user_id_hash.isin(train_users)]

# interactions in validation: here we will not treat differently purchases or
# viewed. If we recommend and it was viewed or purchased, we will considered
# it as a hit
id_cols = ['user_id_hash', 'coupon_id_hash']
df_interactions_valid = pd.concat([df_pva[id_cols], df_vva[id_cols]], ignore_index=True)
df_interactions_valid = (df_interactions_valid.groupby('user_id_hash')
	.agg({'coupon_id_hash': 'unique'})
	.reset_index())
tmp_valid_dict = pd.Series(df_interactions_valid.coupon_id_hash.values,
	index=df_interactions_valid.user_id_hash).to_dict()

valid_coupon_ids = df_coupons_valid_feat.coupon_id_hash.values
keep_users = []
for user, coupons in tmp_valid_dict.items():
	if np.intersect1d(valid_coupon_ids, coupons).size !=0:
		keep_users.append(user)
# out of 6923, we end up with 6070, so not bad
interactions_valid_dict = {k:v for k,v in tmp_valid_dict.items() if k in keep_users}

# Take the 358 validation coupons and the 6070 users seen in training and during
# validation and rank!
left = pd.DataFrame({'user_id_hash':list(interactions_valid_dict.keys())})
left['key'] = 0
right = df_coupons_valid_feat[['coupon_id_hash']]
right['key'] = 0
df_valid = (pd.merge(left, right, on='key', how='outer')
	.drop('key', axis=1))
df_valid = pd.merge(df_valid, df_users_train_feat, on='user_id_hash')
df_valid = pd.merge(df_valid, df_coupons_valid_cat_feat, on = 'coupon_id_hash')
X_valid = (df_valid
	.drop(['user_id_hash','coupon_id_hash'], axis=1)
	.values)
df_eval = df_valid[['user_id_hash','coupon_id_hash']]
# -----------------------------------------------------------------------------

# defining the parameter space
lgb_parameter_space = {
	'learning_rate': hp.uniform('learning_rate', 0.01, 0.5),
	'num_boost_round': hp.quniform('num_boost_round', 50, 500, 50),
	'num_leaves': hp.quniform('num_leaves', 30,1024,5),
    'min_child_weight': hp.quniform('min_child_weight', 1, 50, 2),
    'colsample_bytree': hp.uniform('colsample_bytree', 0.5, 1.),
    'subsample': hp.uniform('subsample', 0.5, 1.),
    'reg_alpha': hp.uniform('reg_alpha', 0.01, 1.),
    'reg_lambda': hp.uniform('reg_lambda', 0.01, 1.),
}

# METHOD1: optimize agains rmse
early_stop_dict = {}
trials = Trials()
lgb_objective.i = 0
best = fmin(fn=lgb_objective,
            space=lgb_parameter_space,
            algo=tpe.suggest,
            max_evals=10,
            trials=trials)
best['num_boost_round'] = early_stop_dict[trials.best_trial['tid']]
best['num_leaves'] = int(best['num_leaves'])
best['verbose'] = -1

# fit model
model = lgb.LGBMRegressor(**best)
model.fit(train,y_train,feature_name=all_cols,categorical_feature=cat_cols)
# save model
# model.booster_.save_model('model.txt')
# to load
# model = lgb.Booster(model_file='mode.txt')

preds = model.predict(X_valid)
df_eval['interest'] = preds
df_ranked = df_eval.sort_values(['user_id_hash', 'interest'], ascending=[False, False])
df_ranked = (df_ranked
	.groupby('user_id_hash')['coupon_id_hash']
	.apply(list)
	.reset_index())
recomendations_dict = pd.Series(df_ranked.coupon_id_hash.values,
	index=df_ranked.user_id_hash).to_dict()

actual = []
pred = []
for k,_ in recomendations_dict.items():
	actual.append(list(interactions_valid_dict[k]))
	pred.append(list(recomendations_dict[k]))

print(mapk(actual,pred))

# METHOD2: optimize against MAP
early_stop_dict = {}
trials = Trials()
lgb_objective_map.i = 0
best = fmin(fn=lgb_objective_map,
            space=lgb_parameter_space,
            algo=tpe.suggest,
            max_evals=5,
            trials=trials)
best['num_boost_round'] = early_stop_dict[trials.best_trial['tid']]
best['num_leaves'] = int(best['num_leaves'])
best['verbose'] = -1
print(1-trials.best_trial['result']['loss'])
# -----------------------------------------------------------------------------

import seaborn as sns
import matplotlib.pyplot as plt
from eli5.sklearn import PermutationImportance
from eli5 import explain_weights_df,explain_prediction_df

# create our dataframe of feature importances
feat_imp_df = explain_weights_df(model, feature_names=all_cols)
feat_imp_df.head(10)

X_train = train.values
exp_pred_df = explain_prediction_df(estimator=model, doc=X_train[0], feature_names=all_cols)

import lime
from lime.lime_tabular import LimeTabularExplainer
explainer = LimeTabularExplainer(X_train, mode='regression',
                                 feature_names=all_cols,
                                 categorical_features=cat_cols,
                                 random_state=1981,
                                 discretize_continuous=True)
exp = explainer.explain_instance(X_valid[10],
                                 model.predict, num_features=20)

import shap

X_valid_rn = X_valid[random.sample(range(X_valid.shape[0]),10000)]
shap_explainer = shap.TreeExplainer(model)
valid_shap_vals = shap_explainer.shap_values(X_valid_rn)
shap.force_plot(valid_shap_vals[0, :], feature_names=all_cols)
shap.force_plot(valid_shap_vals, feature_names=all_cols)
shap.summary_plot(valid_shap_vals, feature_names=all_cols, auto_size_plot=False)
shap.dependence_plot('discount_price_mean', valid_shap_vals, feature_names=all_cols,dot_size=100)
