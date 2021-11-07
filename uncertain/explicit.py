import torch
import numpy as np
from .core import FactorizationModel, VanillaRecommender, UncertainRecommender


def mse(predicted, observed):
    return ((observed - predicted) ** 2).sum()


def gaussian(predicted, observed):
    mean, variance = predicted
    return (((observed - mean) ** 2) / variance).sum() + torch.log(variance).sum()


class Explicit:

    def training_step(self, batch, batch_idx):
        output = self.forward(batch[:, 0].long(), batch[:, 1].long())
        loss = self.loss_func(output, batch[:, 2])
        self.log('train_loss', loss / batch.shape[0])
        return loss

    def validation_step(self, batch, batch_idx):
        output = self.forward(batch[:, 0].long(), batch[:, 1].long())
        loss = self.loss_func(output, batch[:, 2])
        self.log('val_loss', loss / batch.shape[0], prog_bar=True)


class MF(Explicit, FactorizationModel, VanillaRecommender):

    def __init__(self, n_user, n_item, embedding_dim, lr, weight_decay):
        super().__init__(n_user, n_item, embedding_dim, lr, weight_decay, mse)


class CPMF(Explicit, FactorizationModel, UncertainRecommender):

    def __init__(self, n_user, n_item, embedding_dim, lr, weight_decay_MF, weight_decay_gammas):
        super().__init__(n_user, n_item, embedding_dim, lr, weight_decay_MF, gaussian)
        self.init_gammas()
        self.weight_decay_MF = weight_decay_MF
        self.weight_decay_gammas = weight_decay_gammas

    def init_gammas(self):
        self.user_gammas = torch.nn.Embedding(self.n_user, 1)
        self.item_gammas = torch.nn.Embedding(self.n_item, 1)
        torch.nn.init.constant_(self.user_gammas.weight, 1)
        torch.nn.init.constant_(self.item_gammas.weight, 1)
        self.var_activation = torch.nn.Softplus()

    def configure_optimizers(self):
        params = [{'params': self.user_embeddings.parameters(), 'weight_decay': self.weight_decay_MF},
                  {'params': self.item_embeddings.parameters(), 'weight_decay': self.weight_decay_MF},
                  {'params': self.user_gammas.parameters(), 'weight_decay': self.weight_decay_gammas},
                  {'params': self.item_gammas.parameters(), 'weight_decay': self.weight_decay_gammas}]
        return torch.optim.SGD(params, lr=self.lr, momentum=0.9)

    def forward(self, user_ids, item_ids):
        mean = self.dot(user_ids, item_ids)
        user_gamma = self.user_gammas(user_ids)
        item_gamma = self.item_gammas(item_ids)
        var = self.var_activation(user_gamma * item_gamma).flatten()
        return mean, var

    def predict(self, user_ids, item_ids):
        with torch.no_grad():
            mean, var = self(user_ids, item_ids)
            return mean.numpy(), var.numpy()

    def predict_user(self, user):
        with torch.no_grad():
            user_embedding = self.user_embeddings(user)
            mean = (user_embedding * self.item_embeddings.weight).sum(1)
            user_gamma = self.user_gammas(user)
            var = self.var_activation(user_gamma * self.item_gammas.weight).flatten()
            return mean.numpy(), var.numpy()


class OrdRec(Explicit, FactorizationModel, UncertainRecommender):

    def __init__(self, n_user, n_item, score_labels, embedding_dim, lr, weight_decay_MF, weight_decay_step):
        super().__init__(n_user, n_item, embedding_dim, lr, weight_decay_MF)
        self.score_labels = score_labels
        self.init_step()
        self.weight_decay_MF = weight_decay_MF
        self.weight_decay_step = weight_decay_step

    def init_step(self):
        self.user_step = torch.nn.Embedding(self.n_user, len(self.score_labels) - 1)
        self.item_step = torch.nn.Embedding(self.n_item, len(self.score_labels) - 1)
        torch.nn.init.constant_(self.user_step.weight, 0)
        torch.nn.init.constant_(self.item_step.weight, 0)

    def configure_optimizers(self):
        params = [{'params': self.user_embeddings.parameters(), 'weight_decay': self.weight_decay_MF},
                  {'params': self.item_embeddings.parameters(), 'weight_decay': self.weight_decay_MF},
                  {'params': self.user_step.parameters(), 'weight_decay': self.weight_decay_step},
                  {'params': self.item_step.parameters(), 'weight_decay': self.weight_decay_step}]
        return torch.optim.SGD(params, lr=self.lr, momentum=0.9)

    def forward(self, user_ids, item_ids):
        y = self.dot(user_ids, item_ids).reshape(-1, 1)
        steps = torch.exp(self.user_step(user_ids) + self.item_step(item_ids)).cumsum(1)
        distribution = torch.sigmoid(steps - y)
        one = torch.ones((len(distribution), 1), device=distribution.device)
        distribution = torch.cat((distribution, one), 1)
        distribution[:, 1:] -= distribution[:, :-1].clone()
        return distribution

    @staticmethod
    def loss_func(predicted, observed):
        return - predicted[range(len(predicted)), observed.long()].log().sum()

    def _summarize(self, distributions):
        mean = (distributions * self.score_labels).sum(1)
        var = torch.sqrt((distributions * self.score_labels ** 2).sum(1) - mean ** 2)
        return mean.numpy(), var.numpy()

    def predict(self, user_ids, item_ids):
        with torch.no_grad():
            distributions = self.forward(user_ids, item_ids)
            return self._summarize(distributions)

    def predict_user(self, user):
        with torch.no_grad():
            y = (self.user_embeddings(user) * self.item_embeddings.weight).sum(1).reshape(-1, 1)
            steps = torch.exp(self.user_step(user) + self.item_step.weight).cumsum(1)
            distributions = torch.sigmoid(steps - y)
            one = torch.ones((len(distributions), 1), device=distributions.device)
            distributions = torch.cat((distributions, one), 1)
            distributions[:, 1:] -= distributions[:, :-1].clone()
            return self._summarize(distributions)


class BeMF(Explicit, FactorizationModel, UncertainRecommender):

    def __init__(self, n_user, n_item, score_labels, embedding_dim, lr, weight_decay):
        self.score_labels = score_labels
        self.n_scores = len(score_labels)
        super().__init__(n_user, n_item, embedding_dim * self.n_scores, lr, weight_decay)
        self.embedding_dim = int(self.embedding_dim / self.n_scores)
        self.sigmoid = torch.nn.Sigmoid()
        self.softmax = torch.nn.Softmax(dim=1)
        torch.nn.init.uniform_(self.user_embeddings.weight)
        torch.nn.init.uniform_(self.item_embeddings.weight)

    def dot(self, user_ids, item_ids):
        user_embeddings = self.user_embeddings(user_ids)
        item_embeddings = self.item_embeddings(item_ids)
        return (user_embeddings * item_embeddings).view(len(user_ids), self.embedding_dim, self.n_scores).sum(1)

    def forward(self, user_ids, item_ids):
        return self.sigmoid(self.dot(user_ids, item_ids))

    @staticmethod
    def loss_func(predicted, observed):
        positive = predicted[range(len(predicted)), observed.long()]
        negative = (1 - predicted).log().sum()
        return (1 - positive).log().sum() - positive.log().sum() - negative

    def predict(self, user_ids, item_ids):
        with torch.no_grad():
            pred = self.softmax(self(user_ids, item_ids)).max(1)
            return self.score_labels[pred.indices], 1 - pred.values.numpy()

    def predict_user(self, user):
        with torch.no_grad():
            user_embeddings = self.user_embeddings(user)
            dot = (user_embeddings * self.item_embeddings.weight).view(self.n_item, self.embedding_dim, self.n_scores)
            pred = self.softmax(self.sigmoid(dot.sum(1))).max(1)
            return self.score_labels[pred.indices], 1 - pred.values.numpy()


class GMF(Explicit, FactorizationModel, VanillaRecommender):

    def __init__(self, n_user, n_item, embedding_dim, lr):
        super().__init__(n_user, n_item, embedding_dim, lr, 0, mse)
        self.linear = torch.nn.Linear(self.embedding_dim, 1, bias=False)
        torch.nn.init.normal_(self.linear.weight, mean=0, std=0.01)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        return optimizer

    def forward(self, user_ids, item_ids):
        user_embeddings = self.user_embeddings(user_ids)
        item_embeddings = self.item_embeddings(item_ids)
        return self.linear(user_embeddings * item_embeddings).flatten()

    def predict(self, user_ids, item_ids):
        with torch.no_grad():
            return self(user_ids, item_ids).numpy()

    def predict_user(self, user):
        with torch.no_grad():
            user_embedding = self.user_embeddings(user)
            return self.linear(user_embedding * self.item_embeddings.weight).flatten().numpy()


class GaussianGMF(Explicit, FactorizationModel, UncertainRecommender):

    def __init__(self, n_user, n_item, embedding_dim, lr):
        super().__init__(n_user, n_item, embedding_dim, lr, 0, gaussian)
        self.init_net()

    def init_net(self):
        self.linear = torch.nn.Linear(self.embedding_dim, 2, bias=False)
        torch.nn.init.normal_(self.linear.weight, mean=0, std=0.01)
        self.var_activation = torch.nn.Softplus()

    def forward(self, user_ids, item_ids):
        user_embeddings = self.user_embeddings(user_ids)
        item_embeddings = self.item_embeddings(item_ids)
        dot = self.linear(user_embeddings * item_embeddings)
        return dot[:, 0].flatten(), self.var_activation(dot[:, 1]).flatten()

    def predict(self, user_ids, item_ids):
        with torch.no_grad():
            mean, var = self(user_ids, item_ids)
            return mean.numpy(), var.numpy()

    def predict_user(self, user):
        with torch.no_grad():
            user_embedding = self.user_embeddings(user)
            dot = self.linear(user_embedding * self.item_embeddings.weight)
            return dot[:, 0].flatten().numpy(), self.var_activation(dot[:, 1]).flatten().numpy()


class MLP(Explicit, FactorizationModel, VanillaRecommender):

    def __init__(self, n_user, n_item, embedding_dim, lr):
        super().__init__(n_user, n_item, embedding_dim, lr, 0, mse)
        layers = [torch.nn.Linear(self.embedding_dim * 2, self.embedding_dim, bias=True),
                  torch.nn.Linear(self.embedding_dim, int(self.embedding_dim / 2), bias=True),
                  torch.nn.Linear(int(self.embedding_dim / 2), 1, bias=True)]
        self.layers = torch.nn.ModuleList(layers)
        for layer in self.layers:
            torch.nn.init.normal_(layer.weight, mean=0, std=0.01)
            torch.nn.init.normal_(layer.bias, mean=0, std=0.01)
        self.relu = torch.nn.ReLU()

    def net_pass(self, x):
        for layer in self.layers:
            x = self.relu(layer(x))
        return x

    def forward(self, user_ids, item_ids):
        user_embeddings = self.user_embeddings(user_ids)
        item_embeddings = self.item_embeddings(item_ids)
        input_embedding = torch.cat((user_embeddings, item_embeddings), dim=1)
        return self.net_pass(input_embedding).flatten()

    def predict(self, user_ids, item_ids):
        with torch.no_grad():
            return self(user_ids, item_ids).numpy()

    def predict_user(self, user):
        with torch.no_grad():
            user_embedding = self.user_embeddings(user).expand(self.n_item, self.embedding_dim)
            input_embedding = torch.cat((user_embedding, self.item_embeddings.weight), dim=1)
            return self.net_pass(input_embedding).flatten().numpy()


class GaussianMLP(Explicit, FactorizationModel, UncertainRecommender):

    def __init__(self, n_user, n_item, embedding_dim, lr):
        super().__init__(n_user, n_item, embedding_dim, lr, 0, gaussian)
        layers = [torch.nn.Linear(self.embedding_dim * 2, self.embedding_dim, bias=True),
                  torch.nn.Linear(self.embedding_dim, int(self.embedding_dim / 2), bias=True),
                  torch.nn.Linear(int(self.embedding_dim / 2), 2, bias=True)]
        self.layers = torch.nn.ModuleList(layers)
        for layer in self.layers:
            torch.nn.init.normal_(layer.weight, mean=0, std=0.01)
            torch.nn.init.normal_(layer.bias, mean=0, std=0.01)
        self.relu = torch.nn.ReLU()
        self.var_activation = torch.nn.Softplus()

    def net_pass(self, x):
        for layer in self.layers:
            x = self.relu(layer(x))
        return x

    def forward(self, user_ids, item_ids):
        user_embeddings = self.user_embeddings(user_ids)
        item_embeddings = self.item_embeddings(item_ids)
        input_embedding = torch.cat((user_embeddings, item_embeddings), dim=1)
        mean_var = self.net_pass(input_embedding)
        return mean_var[:, 0].flatten(), self.var_activation(mean_var[:, 1]).flatten()

    def predict(self, user_ids, item_ids):
        with torch.no_grad():
            mean, var = self(user_ids, item_ids)
            return mean.numpy(), var.numpy()

    def predict_user(self, user):
        with torch.no_grad():
            user_embedding = self.user_embeddings(user).expand(self.n_item, self.embedding_dim)
            input_embedding = torch.cat((user_embedding, self.item_embeddings.weight), dim=1)
            mean_var = self.net_pass(input_embedding)
            return mean_var[:, 0].flatten().numpy(), self.var_activation(mean_var[:, 1]).flatten().numpy()
