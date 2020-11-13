import argparse
import numpy as np
import pandas as pd
import pickle
import copy
import torch
import torchvision
import matplotlib.pyplot as plt
from time import time
from torchvision import datasets,transforms
from torch import optim
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, TensorDataset
import os
import random
from tqdm import tqdm
import torch.nn.functional as F
from collections import Counter
from itertools import islice
#import shap

def load_dataset():
  train_data = datasets.MNIST(root='./data',train=True,transform=transform,download=True)
  test_data = datasets.MNIST(root='./data',train=False,transform=transform,download=True)
  return train_data, test_data

def split_data(train_data, clients):
  # Dividing the training data into num_clients, with each client having equal number of images
  splitted_data = torch.utils.data.random_split(train_data, [int(train_data.data.shape[0] / clients) for _ in range(clients)])
  return splitted_data

def split_label_wise(train_data):
    label_wise_data = []
    for i in range(10):
        templabeldata = []
        j = 0
        for instance, label in train_data:
            if label == i:
                templabeldata.append(train_data[j])
            j += 1
        label_wise_data.append(templabeldata)
        
    return label_wise_data

def load(train_data, test_data):
  train_loader = [torch.utils.data.DataLoader(x, batch_size=batch_size, shuffle=True) for x in train_data]
  test_loader = torch.utils.data.DataLoader(test_data, batch_size = batch_size, shuffle=True) 

  return train_loader, test_loader

# declare a transformation for MNIST

transform=transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
        ])

# neural network architecture declaration

class Model_MNIST(nn.Module):
  def __init__(self):
    super(Model_MNIST, self).__init__()
    self.conv1 = nn.Conv2d(1, 32, 3, 1)
    self.conv2 = nn.Conv2d(32, 64, 3, 1)
    self.dropout1 = nn.Dropout2d(0.25)
    self.dropout2 = nn.Dropout2d(0.5)
    self.fc1 = nn.Linear(9216, 128)
    self.fc2 = nn.Linear(128, 10)


  def forward(self,x):
    x = self.conv1(x)
    x = F.relu(x)
    x = self.conv2(x)
    x = F.relu(x)
    x = F.max_pool2d(x, 2)
    x = self.dropout1(x)
    x = torch.flatten(x, 1)
    x = self.fc1(x)
    x = F.relu(x)
    x = self.dropout2(x)
    x = self.fc2(x)
    output = F.log_softmax(x, dim=1)
    return output


def client_update(current_local_model, train_loader, optimizer, epoch):

    current_local_model.train()

    for e in range(epoch):
      running_loss = 0
      for batch_idx, (data, target) in enumerate(train_loader):
        optimizer.zero_grad()
        output = current_local_model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()
        running_loss += loss.item()
      #print("Epoch {} - Training loss: {}".format(e,running_loss/len(train_loader)))

    # return client update
    return loss.item()



def server_aggregate(global_model, client_models):
        # aggregate  
    global_dict = global_model.state_dict()   
    for k in global_dict.keys():
        global_dict[k] = torch.stack([client_models[i].state_dict()[k].float() for i in range(len(client_models))], 0).mean(0)
    global_model.load_state_dict(global_dict)
    for model in client_models:
        model.load_state_dict(global_model.state_dict())

def test(model, test_loader, actual_prediction, target_prediction):
    print("Testing")
    model.eval()
    test_loss = 0
    correct = 0
    attack_success_count = 0
    instances = 1
    misclassifications = 0
    with torch.no_grad():
        for data, target in test_loader:
            #print(len(target))
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction='sum').item()  # sum up batch loss
            pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)
    acc = correct / len(test_loader.dataset)

    attack_success_rate = attack_success_count/instances
    attack_success_rate *= 100
    misclassification_rate = misclassifications/instances

    print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset), 100* acc ))
    return test_loss, acc , attack_success_rate, misclassification_rate

#  print the count of label in the data
def getcount_label(data):
  counts = dict()
  for instance,label in data:
    counts[label] = counts.get(label, 0) + 1

  for key, value in counts.items():
    print(key, ':' , value)

# poison client data by flipping labels  
# -1 : poison all labels

def poison_label(client_id, sourcelabel, targetlabel, count_poison, client_data):
  label_poisoned = 0
  client_data[client_id] = list(client_data[client_id])
  i = 0 
  for instance,label in client_data[client_id]:
    client_data[client_id][i] = list(client_data[client_id][i])
    if client_data[client_id][i][1] == sourcelabel:
      client_data[client_id][i][1] = targetlabel
      label_poisoned += 1
    client_data[client_id][i] = tuple(client_data[client_id][i])
    if label_poisoned >= count_poison and count_poison != -1:
      break
    i += 1
  client_data[client_id] = tuple(client_data[client_id])
  return label_poisoned

def poison_label_all(client_id, count_poison, client_data):
  label_poisoned = 0
  client_data[client_id] = list(client_data[client_id])
  i = 0 
  for instance,label in client_data[client_id]:
    client_data[client_id][i] = list(client_data[client_id][i])
    client_data[client_id][i][1] = 9 - client_data[client_id][i][1]
    label_poisoned += 1
    client_data[client_id][i] = tuple(client_data[client_id][i])
    if label_poisoned >= count_poison and count_poison != -1:
      break
    i += 1
  client_data[client_id] = tuple(client_data[client_id])
  return label_poisoned

dataAE = []

def train(num_clients, num_rounds, train_loader, test_loader, losses_train, losses_test, 
          acc_train, acc_test, misclassification_rates, attack_success_rates,communication_rounds, clients_local_updates, global_update,
          source,target):
  # Initialize model and Optimizer

  # Initialize model
  global_model = Model_MNIST()
  global_model_copy = copy.copy(global_model)
  # create K (num_clients)  no. of client_models 
  client_models = [ Model_MNIST() for _ in range(num_clients)]

  # synchronize with global_model
  for model in client_models:
      model.load_state_dict(global_model_copy.state_dict()) # initial synchronizing with global model 
  optimizer = [optim.Adam(model.parameters(), lr=1e-4, weight_decay=1e-5) for model in client_models]

 
  for r in range(num_rounds):
      # client update
      loss = 0
      for i in tqdm(range(num_clients)):
          loss += client_update(client_models[i], train_loader[i],optimizer[i], epoch=epochs)

    

      #calculate dataset for autoencoder
      
      for model in client_models:
        temp = []
        for i in range(len(model.fc1.weight)):
          for j in range(32):
            temp.append(model.fc1.weight[i][j])
        #print('vector size',len(temp))
        dataAE.append(temp)


      #append clinet models and global models at the start of every round

      temp_updates_clients = []
      for i in range(num_clients):
        temp_updates_clients.append(copy.copy(client_models[i]))

      clients_local_updates.append(temp_updates_clients)
      global_update.append(global_model)


      losses_train.append(loss)
      communication_rounds.append(r+1)

      # server aggregate
      server_aggregate(global_model, client_models)

      # calculate test accuracy after the current round
      test_loss, acc ,asr, mcr = test(global_model, test_loader, source, target)
      losses_test.append(test_loss)
      acc_test.append(acc)
      misclassification_rates.append(mcr)
      attack_success_rates.append(asr)
      print("attack success rate : ",asr)
      print("misclassification rate ",mcr)
      #attack_success_rate = asr
      

      print('%d-th round' % (r+1))
      print('average train loss %0.3g | test loss %0.3g | test acc: %0.3f' % (loss / num_clients, test_loss, acc))

  #return attack_success_rate

# federated learning parameters

num_clients = 50         # total number of clients (K)
#num_selected = 6         #  m no of clients (out of K) are selected at radom at each round
num_rounds = 3
epochs = 1              # number of local epoch
batch_size = 32          # local minibatch size
learning_rate = 0.01       # local learning rate

def euclidean_distance(model1,model2):
  # calculating euclidean distance
  d = 0
  for param1, param2 in zip(model1.parameters(),model2.parameters()):
    if len(list(param1.shape)) != 1 and len(list(param2.shape)) != 1:
      temp = torch.cdist(param1.reshape(1,-1), param2,reshape(1,-1))
      d += torch.norm(temp)
      #print(temp)
  print(d)

def run(attackers_id, source_label, poisoned_label,sample_to_poison,client_data, test_data):
  participated_clients = num_clients
  #no_rounds = 2
  total_poisoned_samples = 0
  res_count = sample_to_poison
  #id = 0
  

  #for id in attackers_id:
  #  total_poisoned_samples += poison_label(id,source_label,poisoned_label,sample_to_poison,client_data)
  for id in attackers_id:
    total_poisoned_samples += poison_label_all(id,sample_to_poison,client_data)

  print("samples poisoned: ", total_poisoned_samples)
  train_loader, test_loader = load(client_data, test_data)
  losses_train_p = []
  losses_test_p = []
  acc_train_p = []
  acc_test_p = []
  communication_rounds_p = []
  clients_local_updates_p = []
  global_update_p = []
  misclassification_rates_p = []
  attack_success_rates_p = []
  #attack_success_rate = train(participated_clients,no_rounds,train_loader,test_loader,losses_train_p,losses_test_p,
      #acc_train_p,acc_test_p,misclassification_rates,attack_success_rates,communication_rounds_p,clients_local_updates_p,global_update_p,source_label,poisoned_label)
  

  train(participated_clients,num_rounds,train_loader,test_loader,losses_train_p,losses_test_p,
      acc_train_p,acc_test_p,misclassification_rates_p,attack_success_rates_p,communication_rounds_p,clients_local_updates_p,global_update_p,source_label,poisoned_label)

  print("accuracy",acc_test_p[len(acc_test_p)-1])
  return total_poisoned_samples, attack_success_rates_p, misclassification_rates_p ,acc_test_p, global_update_p, clients_local_updates_p,  communication_rounds_p

global_poison_sample_list = []
global_attack_success_rates_list = []
global_accuracy_list = []
global_client_updates = []
global_global_models = []
global_communication_rounds = []
global_misclassification_rates = []

train_data, test_data = load_dataset()
clients_data = split_data(train_data, num_clients)


print("Deatils of process till now")
print("Poison sample list : ",global_poison_sample_list)
print("Attack success rates :",global_attack_success_rates_list)
print("Accuracy lists : ",global_accuracy_list)
print("misclassifications : ",global_misclassification_rates)

print("Running Baseline Federated Learning")
local_data_fl = copy.copy(clients_data)

attackers = [2]
poisoned_sample, attack_success_rate, misclassification_rates,acc_test, global_updates, client_local_updates, rounds = run(attackers,6,2,-1,local_data_fl, test_data)
global_accuracy_list.append(acc_test)
global_communication_rounds.append(rounds)
global_poison_sample_list.append(poisoned_sample)
global_attack_success_rates_list.append(attack_success_rate)
global_misclassification_rates.append(misclassification_rates)

global_client_updates.append(client_local_updates)
print("Summary")
print("No. of attackers", len(attackers))
print("No. of poisonous samples", poisoned_sample)
print("After training accuracy",acc_test)
print("Attack success rate", attack_success_rate)

#with open('dataAE', 'wb') as fp:
#    pickle.dump(dataAE, fp)





len(dataAE)

trd, tsd = torch.utils.data.random_split(dataAE, [100, 50])

len(trd[0])

class AE(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder_hidden_layer1 = nn.Linear(4096,512)
        self.encoder_hidden_layer2 = nn.Linear(512,128)
        self.encoder_output_layer = nn.Linear(128,32)
        self.decoder_hidden_layer1 = nn.Linear(32,128)
        self.decoder_hidden_layer2 = nn.Linear(128,512)
        self.decoder_output_layer = nn.Linear(512,4096)

    def forward(self, x):
        x = self.encoder_hidden_layer1(x)
        x = torch.relu(x)
        x = self.encoder_hidden_layer2(x)
        x = torch.relu(x)
        x = self.encoder_output_layer(x)
        x = torch.sigmoid(x)
        x = self.decoder_hidden_layer1(x)
        x = torch.relu(x)
        x = self.decoder_hidden_layer2(x)
        x = torch.relu(x)
        x = self.decoder_output_layer(x)
        x = torch.sigmoid(x)
        return x

batch_size = 32
epochs = 20
learning_rate = 1e-3
modelAE = AE()
optimizer = optim.Adam(modelAE.parameters(), lr=learning_rate)
criterion = nn.MSELoss()

import torch.utils.data as data_utils

tr_data = torch.Tensor(trd)

trloader = torch.utils.data.DataLoader(tr_data, batch_size=32, shuffle=True)
teloader = torch.utils.data.DataLoader(tsd, batch_size=32, shuffle=True)

for epoch in range(epochs):
    loss = 0
    for batch_features in trloader:
        optimizer.zero_grad()
        print(type(batch_features))
        outputs = modelAE(batch_features)
        print ("Outputs Shape ", outputs.shape)
        print ("batch_features Shape ", batch_features.shape)
        #train_loss = nn.MSELoss(outputs, batch_features)
        train_loss = criterion(outputs, batch_features)
        train_loss.backward()
        optimizer.step()
        loss += train_loss.item()

    loss = loss / len(trloader)
    

    print("epoch : {}/{}, recon loss = {:.8f}".format(epoch + 1, epochs, loss))
