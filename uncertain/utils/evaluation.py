import torch
import pickle
import numpy as np
import pandas as pd
from scipy import stats
from tqdm.auto import tqdm
from scipy.special import comb
from matplotlib import pyplot as plt
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import KFold
from sklearn.linear_model import LogisticRegression
from uncertain.core import VanillaRecommender, UncertainRecommender


def get_AP(hits):
    n_hits = hits.cumsum(0)
    if n_hits[-1] > 0:
        precision = n_hits / np.arange(1, len(hits) + 1)
        return np.sum(precision * hits) / n_hits[-1]
    else:
        return 0


def rmse(errors):
    return np.sqrt(np.square(errors).mean())


def rpi_score(errors, uncertainties):
    mae = errors.mean()
    errors_deviations = errors - mae
    uncertainties_deviations = uncertainties - uncertainties.mean()
    errors_std = np.abs(errors_deviations).mean()
    epsilon_std = np.abs(uncertainties_deviations).mean()
    num = np.mean(errors * errors_deviations * uncertainties_deviations)
    denom = errors_std * epsilon_std * mae
    return num / denom


def classification(errors, uncertainties):
    splitter = KFold(n_splits=2, shuffle=True, random_state=0)
    targets = errors > 1
    auc = 0
    for train_index, test_index in splitter.split(errors):
        mod = LogisticRegression().fit(uncertainties[train_index].reshape(-1, 1), targets[train_index])
        prob = mod.predict_proba(uncertainties[test_index].reshape(-1, 1))
        auc += roc_auc_score(targets[test_index], prob[:, 1]) / 2
    return auc


def quantile_score(errors, uncertainties):
    quantiles = np.quantile(uncertainties, np.linspace(0, 1, 21))
    q_rmse = np.zeros(20)
    for idx in range(20):
        ind = np.bitwise_and(quantiles[idx] <= uncertainties, uncertainties < quantiles[idx + 1])
        q_rmse[idx] = np.sqrt(np.square(errors[ind]).mean())
    return q_rmse


def accuracy_metrics(size, max_k, is_uncertain):
    out = {'MAP': np.zeros((size, max_k)),
           'Recall': np.zeros((size, max_k))}
    if is_uncertain:
        out['URI_rec'] = np.full(size, np.nan)
    return out


def test(model, data, max_k, name, threshold=4):
    '''
    Code for explicit data
    '''
    metrics = {}
    Rating_rec = accuracy_metrics(len(data.test_users), max_k, isinstance(model, UncertainRecommender))

    pred = model.predict(torch.tensor(data.test[:, 0]).long(), torch.tensor(data.test[:, 1]).long())
    if not isinstance(model, UncertainRecommender):
        pred = pred[~np.isnan(pred)]
        metrics['RMSE'] = rmse(pred - data.test[:, 2])
    if isinstance(model, UncertainRecommender):
        idx = ~np.isnan(pred[1])
        unc = pred[1][idx]
        pred = pred[0][idx]
        errors = np.abs(data.test[:, 2] - pred)
        metrics['RMSE'] = np.sqrt(np.square(errors).mean())
        metrics['RPI'] = rpi_score(errors, unc)
        metrics['Classification'] = classification(errors, unc)
        metrics['Quantile RMSE'] = quantile_score(errors, unc)
        metrics['Pearson error x unc'] = stats.pearsonr(errors, unc)[0]
        metrics['Spearman error x unc'] = stats.spearmanr(errors, unc)[0]
        metrics['dw'] = metrics['Quantile RMSE'][-1] - metrics['Quantile RMSE'][0]
        URI_rat = {'test': {'avg': unc.mean(), 'std': unc.std()},
                   'rec': {'hits': 0, 'hits_unc': 0,
                           'avg': np.zeros(len(data.test_users))}}

        rand_preds = model.predict(data.rand['users'], data.rand['items'])
        idx = ~np.isnan(rand_preds[1])
        unc = rand_preds[1][idx]
        user_vars = data.user.loc[data.rand['users'][idx]]
        item_vars = data.item.loc[data.rand['items'][idx]]
        metrics['User_unc_corr'] = {column: stats.spearmanr(unc, user_vars[column].to_numpy().flatten())[0]
                                    for column in data.user.columns}
        metrics['Item_unc_corr'] = {column: stats.spearmanr(unc, item_vars[column].to_numpy().flatten())[0]
                                    for column in data.item.columns}

        Cuts = {'Values': np.nanquantile(rand_preds[1], np.linspace(0.8, 0.2, 4)),
                'Coverage': np.ones((len(data.test_users), 5)),
                'Relevance': np.zeros((len(data.test_users), 5)),
                'MAP': np.zeros((len(data.test_users), 5)),
                'Surprise': np.zeros((len(data.test_users), 5))}

    if hasattr(model, 'uncertain_predict_user'):
        Uncertain_rec = accuracy_metrics(len(data.test_users), max_k, False)
        unc = 1 - model.uncertain_predict(torch.tensor(data.test[:, 0]).long(), torch.tensor(data.test[:, 1]).long(), threshold=threshold)
        URI_unc = {'test': {'avg': unc.mean(), 'std': unc.std()},
                   'rec': {'hits': 0, 'hits_unc': 0, 'avg': np.zeros(len(data.test_users))}}

    precision_denom = np.arange(1, max_k + 1)

    for idxu, user in enumerate(tqdm(data.test_users, desc=name+' - Recommending')):
        targets = data.test[data.test[:, 0] == user]
        targets = targets[targets[:, 2] >= 4, 1]
        rated = data.train_val.item[data.train_val.user == user].to_numpy()
        rec = model.recommend(user, remove_items=rated, n=data.n_item - len(rated))
        top_k = rec[:max_k]
        hits = top_k.index.isin(targets)
        n_hits = hits.cumsum(0)

        if n_hits[-1] > 0:
            precision = n_hits / precision_denom
            Rating_rec['MAP'][idxu] = np.cumsum(precision * hits) / np.maximum(1, n_hits)
            Rating_rec['Recall'][idxu] = n_hits / len(targets)

        if isinstance(model, UncertainRecommender):
            unc = rec.uncertainties.to_numpy()
            top_k_unc = unc[:max_k]
            URI_rat['rec']['avg'][idxu] = top_k_unc.mean()
            hits_unc = np.sum(top_k_unc * hits)
            URI_rat['rec']['hits'] += n_hits[-1]
            URI_rat['rec']['hits_unc'] += hits_unc
            if n_hits[-1] > 0:
                avg_hits_unc = hits_unc / n_hits[-1]
                Rating_rec['URI_rec'][idxu] = (URI_rat['rec']['avg'][idxu] - avg_hits_unc) / top_k_unc.std()

            Cuts['MAP'][idxu, 0] = Rating_rec['MAP'][idxu][-1]
            Cuts['Relevance'][idxu, 0] = top_k['scores'].mean()
            profile_distances = data.distances[top_k.index][:, rated] * hits[:, np.newaxis]
            Cuts['Surprise'][idxu, 0] = profile_distances.min(1).sum(0) / max_k

            for idxc, cut in enumerate(Cuts['Values']):
                if np.sum(top_k_unc < cut) == len(top_k_unc):
                    Cuts['MAP'][idxu, idxc + 1] = Cuts['MAP'][idxu, idxc]
                    Cuts['Coverage'][idxu, idxc + 1] = Cuts['Coverage'][idxu, idxc]
                    Cuts['Relevance'][idxu, idxc + 1] = Cuts['Relevance'][idxu, idxc]
                    Cuts['Surprise'][idxu, idxc + 1] = Cuts['Surprise'][idxu, idxc]
                else:
                    top_k_constrained = rec[unc < cut][:max_k]
                    top_k_unc = top_k_constrained.uncertainties.to_numpy()
                    Cuts['Coverage'][idxu, idxc + 1] = len(top_k_unc) / max_k
                    if len(top_k_constrained > 0):
                        Cuts['Relevance'][idxu, idxc + 1] = top_k_constrained['scores'].mean()
                        hits_ = top_k_constrained.index.isin(targets)
                        if hits_.sum() > 0:
                            Cuts['MAP'][idxu, idxc + 1] = get_AP(hits_)
                            profile_distances = data.distances[top_k_constrained.index][:, rated] * hits_[:, np.newaxis]
                            Cuts['Surprise'][idxu, idxc + 1] = profile_distances.min(1).sum(0) / len(top_k_constrained)

        if hasattr(model, 'uncertain_predict_user'):
            top_k_unc = model.uncertain_recommend(user, threshold=threshold, remove_items=rated, n=max_k)
            top_k_unc_unc = 1 - top_k_unc.scores.to_numpy()
            URI_unc['rec']['avg'][idxu] = top_k_unc_unc.mean()
            if np.sum(top_k_unc.index.to_numpy() == top_k.index.to_numpy()) == max_k:
                Uncertain_rec['MAP'][idxu] = Rating_rec['MAP'][idxu]
                Uncertain_rec['Recall'][idxu] = Rating_rec['Recall'][idxu]
            else:
                hits = top_k_unc.index.isin(targets)
                n_hits = hits.cumsum(0)
            URI_unc['rec']['hits'] += n_hits[-1]
            URI_unc['rec']['hits_unc'] += np.sum(top_k_unc_unc * hits)

            if n_hits[-1] > 0:
                precision = n_hits / precision_denom
                Uncertain_rec['MAP'][idxu] = np.cumsum(precision * hits) / np.maximum(1, n_hits)
                Uncertain_rec['Recall'][idxu] = n_hits / len(targets)

    metrics['Rating_rec'] = {'MAP': np.mean(Rating_rec['MAP'], 0),
                             'Recall': np.mean(Rating_rec['Recall'], 0)}

    if isinstance(model, UncertainRecommender):
        avg_unc_hits = URI_rat['rec']['hits_unc'] / URI_rat['rec']['hits']
        metrics['Rating_rec']['URI_global'] = (URI_rat['test']['avg'] - avg_unc_hits) / URI_rat['test']['std']
        metrics['Rating_rec']['Unc_MAP_corr'] = stats.spearmanr(Rating_rec['MAP'][:, -1], URI_rat['rec']['avg'])[0]
        metrics['Rating_rec']['URI_rec'] = np.nanmean(Rating_rec['URI_rec'])

        metrics['Cuts'] = {'Values': Cuts['Values'],
                           'Coverage': Cuts['Coverage'].mean(0),
                           'Relevance': Cuts['Relevance'].mean(0),
                           'MAP': Cuts['MAP'].mean(0),
                           'Surprise': Cuts['Surprise'].mean(0),
                           'Map*': np.array([Cuts['MAP'][Cuts['Coverage'][:, k] == 1, k].mean() for k in range(5)]),
                           'Surprise*': np.array([Cuts['Surprise'][Cuts['Coverage'][:, k] == 1, k].mean() for k in range(5)])}

    if hasattr(model, 'uncertain_predict_user'):
        metrics['Uncertain_rec'] = {'MAP': np.mean(Uncertain_rec['MAP'], 0),
                                    'Recall': np.mean(Uncertain_rec['Recall'], 0)}
        avg_unc_hits = URI_unc['rec']['hits_unc'] / URI_unc['rec']['hits']
        metrics['Uncertain_rec']['URI_global'] = (URI_unc['test']['avg'] - avg_unc_hits) / URI_unc['test']['std']
        metrics['Uncertain_rec']['Unc_MAP_corr'] = stats.spearmanr(Uncertain_rec['MAP'][:, -1], URI_unc['rec']['avg'])[0]

    if name is not None:
        with open('results/' + name + '.pkl', 'wb') as f:
            pickle.dump(metrics, file=f)

    return metrics


def test_vanilla(model, data, max_k, name):
    '''
    Code for non-uncertainty aware implicit models
    '''
    pred = model.predict(data.test[:, 0], data.test[:, 1])
    neg = model.predict(data.test[:, 0], data.rand['items'])
    
    is_concordant = pred - neg > 0
    metrics = {'FCP': is_concordant.sum().item() / len(data.test)}
    
    precision_denom = torch.arange(1, max_k+1)
    MAP = torch.zeros((data.n_user, max_k))
    Recall = torch.zeros((data.n_user, max_k))
    Average_Surprise = np.zeros((data.n_user, max_k))
    MAP_Relative = np.zeros((data.n_user, max_k))
    Recall_Relative = np.zeros((data.n_user, max_k))

    for idxu, user in enumerate(tqdm(range(data.n_user), desc=name+' - Recommending')):
        
        targets = torch.tensor(data.test[data.test[:, 0] == user, 1])
        rated_train = torch.tensor(data.train[:, 1][data.train[:, 0] == user])
        rated_val = torch.tensor(data.val[:, 1][data.val[:, 0] == user])
        rated = torch.cat([rated_train, rated_val])
        
        rec, score = model.rank(torch.tensor(user), ignored_item_ids=rated, top_n=max_k)
        hits = torch.isin(rec, targets, assume_unique=True)
        n_hits = hits.cumsum(0)
        
        if n_hits[-1] > 0:
            precision = n_hits / precision_denom
            MAP[idxu] = torch.cumsum(precision * hits, 0) / torch.clamp(n_hits, min=1)
            Recall[idxu] = n_hits / len(targets)
            # Surprise
            if hasattr(data, 'distances'):
                profile_distances = data.distances[rec[:max_k]][:, rated]  # To use expected surprise, add: * hits[:, np.newaxis]
                Average_Surprise[idxu] = profile_distances.min(1).cumsum(0) / np.arange(1, max_k+1)

        # Evaluate tail relative
        n_target_tail = torch.isin(targets, data.tail_items).sum()

        if n_target_tail > 0:
            # Dont recommend head items
            score[torch.isin(rec, data.head_items, assume_unique=True)] = -99999999
            rec = rec[torch.argsort(score, descending=True)]
            hits = torch.isin(rec[:max_k], targets, assume_unique=True)
            n_hits = hits.cumsum(0)

            if n_hits[-1] > 0:
                Recall_Relative[idxu] = n_hits / n_target_tail
                precision = n_hits / precision_denom
                MAP_Relative[idxu] = torch.cumsum(precision * hits, 0) / torch.clamp(n_hits, min=1)

        else:
            Recall_Relative[idxu] = np.nan
            MAP_Relative[idxu] = np.nan
            

    metrics['MAP'] = MAP.mean(0).numpy()
    metrics['Recall'] = Recall.mean(0).numpy()
    metrics['Average_Surprise'] = Average_Surprise.mean(0)
    print(np.isnan(MAP_Relative).sum())
    metrics['Map relative'] = np.nanmean(MAP_Relative, 0)
    metrics['Recall relative'] = np.nanmean(Recall_Relative, 0)
    
    with open('results/' + name + '.pkl', 'wb') as f:
        pickle.dump(metrics, file=f)
    
    return metrics


def test_uncertain(model, data, max_k, name):
    '''
    Code for uncertainty-aware implicit models
    '''
    pred = model.predict(data.test[:, 0], data.test[:, 1])
    neg = model.predict(data.test[:, 0], data.rand['items'])
    
    is_concordant = pred[0] - neg[0] > 0
    metrics = {'FCP': is_concordant.sum().item() / len(data.test)}
    
    rand_preds = model.predict(data.rand['users'], data.rand['items'])
    metrics['corr_usup'] = stats.spearmanr(rand_preds[1], data.user_support[data.rand['users']].flatten())[0]
    metrics['corr_isup'] = stats.spearmanr(rand_preds[1], data.item_support[data.rand['items']].flatten())[0]

    f, ax = plt.subplots(ncols=2)
    ax[0].hist(rand_preds[1], density=True)
    ax[0].set_xlabel('Uncertainty')
    ax[0].set_ylabel('Density')
    ax[1].plot(rand_preds[0], rand_preds[1], 'o')
    ax[1].set_xlabel('Relevance')
    ax[1].set_ylabel('Uncertainty')
    f.tight_layout()
    f.savefig(f'plots/{name}.pdf')
    
    precision_denom = torch.arange(1, max_k+1)
    MAP = torch.zeros((data.n_user, max_k))
    Recall = torch.zeros((data.n_user, max_k))
    avg_unc = torch.zeros(data.n_user)
    URI = torch.full((data.n_user,), float('nan'))
    Average_Surprise = np.zeros((data.n_user, max_k))
    
    if hasattr(model, 'uncertain_transform'):
        MAP2 = torch.zeros((data.n_user, max_k))
        Recall2 = torch.zeros((data.n_user, max_k))
        Average_Surprise2 = np.zeros((data.n_user, max_k))
        
    alphas = np.linspace(-1, 1, 11)
    unc_MAP = torch.zeros((data.n_user, len(alphas)))
    metrics['norm_unc'] = [[], []]

    Cuts = {'Values': np.nanquantile(rand_preds[1], np.linspace(0.8, 0.2, 4)),
            'Coverage': np.ones((data.n_user, 5)),
            'Relevance': np.zeros((data.n_user, 5)),
            'MAP': np.zeros((data.n_user, 5)),
            'Surprise': np.zeros((data.n_user, 5))}

    for idxu, user in enumerate(tqdm(range(data.n_user), desc=name+' - Recommending')):
        
        targets = torch.tensor(data.test[data.test[:, 0] == user, 1])
        rated_train = torch.tensor(data.train[:, 1][data.train[:, 0] == user])
        rated_val = torch.tensor(data.val[:, 1][data.val[:, 0] == user])
        rated = torch.cat([rated_train, rated_val])
        
        rec, score, unc = model.rank(torch.tensor(user), ignored_item_ids=rated, top_n=999999)
        
        hits = torch.isin(rec[:max_k], targets, assume_unique=True)
        n_hits = hits.cumsum(0)
        avg_unc[idxu] = unc[:max_k].mean()
        
        if n_hits[-1] > 0:
            precision = n_hits / precision_denom
            MAP[idxu] = torch.cumsum(precision * hits, 0) / torch.clamp(n_hits, min=1)
            Recall[idxu] = n_hits / len(targets)
            URI[idxu] = (avg_unc[idxu] - unc[:max_k][hits].mean()) / unc[:max_k].std()
            # Surprise
            if hasattr(data, 'distances'):
                profile_distances = data.distances[rec[:max_k]][:, rated]  # To use expected surprise, add: * hits[:, np.newaxis]
                Average_Surprise[idxu] = profile_distances.min(1).cumsum(0) / np.arange(1, max_k+1)

        Cuts['MAP'][idxu, 0] = MAP[idxu][-1]
        Cuts['Relevance'][idxu, 0] = score[:max_k].mean()

        top_k_unc = unc[:max_k]
        for idxc, cut in enumerate(Cuts['Values']):
            if torch.sum(top_k_unc < cut) == len(top_k_unc):
                Cuts['MAP'][idxu, idxc + 1] = Cuts['MAP'][idxu, idxc]
                Cuts['Coverage'][idxu, idxc + 1] = Cuts['Coverage'][idxu, idxc]
                Cuts['Relevance'][idxu, idxc + 1] = Cuts['Relevance'][idxu, idxc]
            else:
                top_k_constrained = rec[unc < cut][:max_k]
                top_k_unc = unc[unc < cut][:max_k]
                top_k_scores = score[unc < cut][:max_k]
                Cuts['Coverage'][idxu, idxc + 1] = len(top_k_unc) / max_k
                if len(top_k_constrained > 0):
                    Cuts['Relevance'][idxu, idxc + 1] = top_k_scores.mean()
                    hits_ = torch.isin(top_k_constrained, targets, assume_unique=True)
                    n_hits = hits_.cumsum(0)
                    if hits_.sum() > 0:
                        precision = n_hits / precision_denom[:len(n_hits)]
                        map_k = torch.cumsum(precision * hits_, 0) / torch.clamp(n_hits, min=1)
                        Cuts['MAP'][idxu, idxc + 1] = map_k.numpy()[-1]
            
        if hasattr(model, 'uncertain_transform'):
            score_unc = model.uncertain_transform((score, unc))
            rec_unc = rec[torch.argsort(score_unc, descending=True)]
            hits = torch.isin(rec_unc[:max_k], targets, assume_unique=True)
            n_hits = hits.cumsum(0)
            
            if n_hits[-1] > 0:
                precision = n_hits / precision_denom
                MAP2[idxu] = torch.cumsum(precision * hits, 0) / torch.clamp(n_hits, min=1)
                Recall2[idxu] = n_hits / len(targets)
                # Surprise
                if hasattr(data, 'distances'):
                    profile_distances = data.distances[rec[:max_k]][:, rated]  # To use expected surprise, add: * hits[:, np.newaxis]
                    Average_Surprise2[idxu] = profile_distances.min(1).cumsum(0) / np.arange(1, max_k+1)
            
        norm_unc = (unc[:max_k] - unc[:max_k].mean()) / unc[:max_k].std()
        metrics['norm_unc'][0] += norm_unc[hits].tolist()
        metrics['norm_unc'][1] += norm_unc[~hits].tolist()

        # Unc rank
        for idxa, alpha in enumerate(alphas):
            score_unc = score + alpha * unc
            rec_unc = rec[torch.flip(np.argsort(score_unc), [0])[:max_k]]
            hits = torch.isin(rec_unc, targets, assume_unique=True)
            
            if hits.sum() > 0:
                precision = hits.cumsum(0) / precision_denom
                unc_MAP[idxu, idxa] = torch.sum(precision * hits) / hits.sum()
        
    metrics['MAP'] = MAP.mean(0).numpy()
    metrics['Recall'] = Recall.mean(0).numpy()
    metrics['UAC'] = stats.spearmanr(MAP[:, -1], avg_unc)[0]
    metrics['URI'] = torch.nanmean(URI, 0).item()
    metrics['unc_MAP'] = unc_MAP.mean(0).numpy()
    metrics['Average_Surprise'] = Average_Surprise.mean(0)

    metrics['Cuts'] = {'Values': Cuts['Values'],
                       'Coverage': Cuts['Coverage'].mean(0),
                       'Relevance': Cuts['Relevance'].mean(0),
                       'MAP': Cuts['MAP'].mean(0),
                       'Surprise': Cuts['Surprise'].mean(0),
                       'Map*': np.array([Cuts['MAP'][Cuts['Coverage'][:, k] == 1, k].mean() for k in range(5)]),
                       'Surprise*': np.array([Cuts['Surprise'][Cuts['Coverage'][:, k] == 1, k].mean() for k in range(5)])}

    
    if hasattr(model, 'uncertain_transform'):
        metrics['MAP2'] = MAP2.mean(0).numpy()
        metrics['Recall2'] = Recall2.mean(0).numpy()
        metrics['Average_Surprise2'] = Average_Surprise2.mean(0)
    
    # MAP and surprise per uncertainty bin
    quantiles = np.quantile(avg_unc, np.linspace(0.1, 1, 11))
    metrics['MAP-Uncertainty'] = np.zeros(len(quantiles) - 1)
    metrics['Surprise-Uncertainty'] = np.zeros(len(quantiles) - 1)
    for i in range(10):
        indexer = np.logical_and(avg_unc >= quantiles[i], avg_unc <= quantiles[i+1])
        metrics['MAP-Uncertainty'][i] = MAP[indexer, -1].mean()
        metrics['Surprise-Uncertainty'][i] = Average_Surprise[indexer, -1].mean()
    
    # MAP and Surprise per user profile size bin
    quantiles = np.quantile(data.user_support, np.linspace(0.1, 1, 11))
    metrics['MAP-ProfSize'] = np.zeros(len(quantiles) - 1)
    metrics['Surprise-ProfSize'] = np.zeros(len(quantiles) - 1)
    for i in range(10):
        indexer = np.logical_and(data.user_support >= quantiles[i], data.user_support <= quantiles[i+1])
        metrics['MAP-ProfSize'][i] = MAP[indexer, -1].mean()
        metrics['Surprise-ProfSize'][i] = Average_Surprise[indexer].mean()
    
    '''
    # MAP per user uncertainty (for CPMF)
    if hasattr(model, 'get_user_uncertainty'):
        uncertainty = model.get_user_uncertainty()
        quantiles = np.quantile(uncertainty, np.linspace(0.1, 1, 11))
        metrics['MAP-UserUncertainty'] = np.zeros(len(quantiles) - 1)
        for i in range(10):
            idxrer = np.logical_and(uncertainty >= quantiles[i], uncertainty <= quantiles[i+1])
            metrics['MAP-UserUncertainty'][i] = MAP[idxrer, -1].mean()
    '''
    
    with open('results/' + name + '.pkl', 'wb') as f:
        pickle.dump(metrics, file=f)
    
    return metrics



def test_chap_five(model, data, max_k, name, debug=False):
    '''
    Code for uncertainty-aware implicit models
    '''
    pred = model.predict(data.test[:, 0], data.test[:, 1])
    neg = model.predict(data.test[:, 0], data.rand['items'])
    
    is_concordant = pred[0] - neg[0] > 0
    metrics = {'FCP': is_concordant.sum().item() / len(data.test)}
    
    rand_preds = model.predict(data.rand['users'], data.rand['items'])
    # Fix potential bugs
    bug_idx = rand_preds[1] < 10
    rand_preds = rand_preds[0][bug_idx], rand_preds[1][bug_idx]
    if not debug:
        rand_preds = rand_preds[0], np.exp(rand_preds[1])
    metrics['corr_usup'] = stats.spearmanr(rand_preds[1], data.user_support[data.rand['users'][bug_idx]].flatten())[0]
    metrics['corr_isup'] = stats.spearmanr(rand_preds[1], data.item_support[data.rand['items'][bug_idx]].flatten())[0]

    f, ax = plt.subplots(ncols=2)
    ax[0].hist(rand_preds[1], density=True)
    ax[0].set_xlabel('Uncertainty')
    ax[0].set_ylabel('Density')
    ax[1].plot(rand_preds[0][:1000], rand_preds[1][:1000], 'o')
    ax[1].set_xlabel('Relevance')
    ax[1].set_ylabel('Uncertainty')
    f.tight_layout()
    f.savefig(f'plots/{name}.pdf')
    
    precision_denom = torch.arange(1, max_k+1)
    ratio_grid = np.linspace(0, 1, 11)
    Results = {'Recall': np.zeros((data.n_user, len(ratio_grid), max_k)),
               'MAP': np.zeros((data.n_user, len(ratio_grid), max_k)),
               'Surprise': np.zeros((data.n_user, len(ratio_grid), max_k)),
               'MAP relative': np.zeros((data.n_user, len(ratio_grid), max_k)),
               'Recall relative': np.zeros((data.n_user, len(ratio_grid), max_k))}

    for idxu, user in enumerate(tqdm(range(data.n_user), desc=name+' - Recommending')):
        
        targets = torch.tensor(data.test[data.test[:, 0] == user, 1])
        targets_relative = targets[torch.isin(targets, data.tail_items, assume_unique=True)]
        
        rated_train = torch.tensor(data.train[:, 1][data.train[:, 0] == user])
        rated_val = torch.tensor(data.val[:, 1][data.val[:, 0] == user])
        
        rated = torch.cat([rated_train, rated_val])

        model.ratio = 1
        rec, score, unc = model.rank(torch.tensor(user), ignored_item_ids=rated, top_n=999999)

        for idxr, ratio in enumerate(ratio_grid):
            if not debug:
                score_ = ratio * score + (1 - ratio) * unc.exp().sqrt()
            else:
                score_ = ratio * score + (1 - ratio) * unc.sqrt()
            rec_ = rec[torch.argsort(score_, descending=True)]
            
            hits = torch.isin(rec_[:max_k], targets, assume_unique=True)
            n_hits = hits.cumsum(0)
        
            if n_hits[-1] > 0:
                Results['Recall'][idxu, idxr] = n_hits / len(targets)
                precision = n_hits / precision_denom
                Results['MAP'][idxu, idxr] = torch.cumsum(precision * hits, 0) / torch.clamp(n_hits, min=1)
                # Surprise
                if hasattr(data, 'distances'):
                    profile_distances = data.distances[rec[:max_k]][:, rated]  # To use expected surprise, add: * hits[:, np.newaxis]
                    Results['Surprise'][idxu, idxr] = profile_distances.min(1).cumsum(0) / np.arange(1, max_k+1)

            # Evaluate tail relative
            n_target_tail = torch.isin(targets, data.tail_items).sum()

            if n_target_tail > 0:
                # Dont recommend head items
                score_[torch.isin(rec_, data.head_items, assume_unique=True)] = -99999999
                rec_ = rec[torch.argsort(score_, descending=True)]
                hits = torch.isin(rec_[:max_k], targets, assume_unique=True)
                n_hits = hits.cumsum(0)
    
                if n_hits[-1] > 0:
                    Results['Recall relative'][idxu, idxr] = n_hits / n_target_tail
                    precision = n_hits / precision_denom
                    Results['MAP relative'][idxu, idxr] = torch.cumsum(precision * hits, 0) / torch.clamp(n_hits, min=1)

            else:
                Results['Recall relative'][idxu, idxr] = np.nan
                Results['MAP relative'][idxu, idxr] = np.nan

    metrics['Recall'] = Results['Recall']
    metrics['MAP'] = Results['MAP']
    metrics['Surprise'] = Results['Surprise']
    metrics['Map relative'] = Results['MAP relative']
    metrics['Recall relative'] = Results['Recall relative']
                     
    with open('results/' + name + '.pkl', 'wb') as f:
        pickle.dump(metrics, file=f)
    
    return metrics
