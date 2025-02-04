# Author: Guillaume Calmettes, PhD
# University of Los Angeles California

import os
import sys
import re
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib import pyplot as plt
import numpy as np
import pandas as pd
import scipy.stats as stats
from scipy import optimize
from itertools import groupby


# from https://gist.github.com/walkermatt/2871026
from threading import Timer

def debounce(wait):
  """ Decorator that will postpone a functions
      execution until after wait seconds
      have elapsed since the last time it was invoked. """
  def decorator(fn):
    def debounced(*args, **kwargs):
      def call_it():
        fn(*args, **kwargs)
      try:
        debounced.t.cancel()
      except(AttributeError):
        pass
      debounced.t = Timer(wait, call_it)
      debounced.t.start()
    return debounced
  return decorator


#############################################################################
# --------- Natural Abundance Correction CLASS -----------------------------#
#############################################################################

class NAProcess:
  # Adapted from IsoCor code (https://github.com/MetaSys-LISBP/IsoCor)
  
  ##################
  ## Init and setup
  ##################

  def __init__(self, entry, atomTracer="H", purityTracer=[0, 1], FAMES=True, CHOL=False):
    self.NaturalAbundanceDistributions = self.__getNaturalAbundanceDistributions()
    self.formula = self.getFAFormulaString(entry, FAMES, CHOL)
    self.elementsDict = self.parseFormula(self.formula)
    self.atomTracer = atomTracer
    self.purityTracer = purityTracer
    self.correctionMatrix = self.computeCorrectionMatrix(self.elementsDict, self.atomTracer, self.NaturalAbundanceDistributions, purityTracer)
    
  def getFAFormulaString(self, entry, FAMES, CHOL=False):
    ''' Return formula string e.g.: C3H2O3'''
    regex = "C([0-9]+):([0-9]+)"
    carbon,doubleBond = [int(val) for val in re.findall(regex, entry)[0]]
    hydrogen = 3+(carbon-2)*2+1-2*doubleBond
    oxygen = 2
    silicon = 0
    if (FAMES):
      carbon=carbon+1
      hydrogen=hydrogen-1+3
    if (CHOL):
      carbon, hydrogen, oxygen, silicon = 30, 54, 1, 1 
    return "".join(["".join([letter,str(n)]) for [letter,n] in [
              ["C", carbon], 
              ["H", hydrogen],
              ["Si", silicon],
              ["O", oxygen]] if n>0])

  def parseFormula(self, formula):
    """
    Parse the elemental formula and return the number
    of each element in a dictionnary d={'El_1':x,'El_2':y,...}.
    """
    regex = f"({'|'.join(self.NaturalAbundanceDistributions.keys())})([0-9]{{0,}})"
    elementDict = dict((element, 0) for element in self.NaturalAbundanceDistributions.keys())
    for element,n in re.findall(regex, formula):
      if n:
        elementDict[element] += int(n)
      else:
        elementDict[element] += 1
    return elementDict

  def __getNaturalAbundanceDistributions(self):
    '''Return a dictionary of the isotopic proportions at natural abundance 
    desribed in https://www.ncbi.nlm.nih.gov/pubmed/27989585'''
    H1, H2 = 0.999885, 0.000115
    C12, C13 = 0.9893, 0.0107
    N14, N15 = 0.99632, 0.00368
    O16, O17, O18 = 0.99757, 0.00038, 0.00205
    Si28, Si29, Si30 = 0.922297, 0.046832, 0.030872
    S32, S33, S34, S36 = 0.9493, 0.0076, 0.0429, 0.0002

    return {'H': np.array([H1, H2]), # hydrogen
            'C': np.array([C12, C13]), # carbon
            'N': np.array([N14, N15]), # nitrogen
            'O': np.array([O16, O17, O18]), # oxygen
            'Si': np.array([Si28, Si29, Si30]), # silicon
            'S': np.array([S32, S33, S34, S36])} # sulphur

  def __calculateMassDistributionVector(self, elementDict, atomTracer, NADistributions):
    """
    Calculate a mass distribution vector (at natural abundancy),
    based on the elemental compositions of metabolite.
    The element corresponding to the isotopic tracer is not taken
    into account in the metabolite moiety.
    """
    result = np.array([1.])
    for atom,n in elementDict.items():
      if atom not in [atomTracer]:
        for i in range(n):
          result = np.convolve(result, NADistributions[atom])
    return result

  def computeCorrectionMatrix(self, elementDict, atomTracer, NADistributions, purityTracer):
    # calculate correction vector used for correction matrix construction
    # it corresponds to the mdv at natural abundance of all elements except the
    # isotopic tracer
    correctionVector = self.__calculateMassDistributionVector(elementDict, atomTracer, NADistributions)

    # check if the isotopic tracer is present in formula
    try:
      nAtomTracer =  elementDict[atomTracer]
    except: 
      print("The isotopic tracer must to be present in the metabolite formula!")

    tracerNADistribution =  NADistributions[atomTracer]

    m = 1+nAtomTracer*(len(tracerNADistribution)-1)
    c = len(correctionVector)

    if m > c + nAtomTracer*(len(tracerNADistribution)-1):
      print("There might be a problem in matrix size.\nFragment does not contains enough atoms to generate this isotopic cluster.")

    if c < m:
      # padd with zeros
      correctionVector.resize(m)

    # create correction matrix
    correctionMatrix = np.zeros((m, nAtomTracer+1))
    for i in range(nAtomTracer+1):
      column = correctionVector[:m]
      for na in range(i):
        column = np.convolve(column, purityTracer)[:m]

      for nb in range(nAtomTracer-i):
        column = np.convolve(column, tracerNADistribution)[:m]                    
      correctionMatrix[:,i] = column
    return correctionMatrix

  ##################
  ## Data processing
  ##################

  def _computeCost(self, currentMID, target, correctionMatrix):
    """
    Cost function used for BFGS minimization.
        return : (sum(target - correctionMatrix * currentMID)^2, gradient)
    """
    difference = target - np.dot(correctionMatrix, currentMID)
    # calculate sum of square differences and gradient
    return (np.dot(difference, difference), np.dot(correctionMatrix.transpose(), difference)*-2)

  def _minimizeCost(self, args):
    '''
    Wrapper to perform least-squares optimization via the limited-memory 
    Broyden-Fletcher-Goldfarb-Shanno algorithm, with an explicit lower boundary
    set to zero to eliminate any potential negative fractions.
    '''
    costFunction, initialMID, target, correctionMatrix = args
    res = optimize.minimize(costFunction, initialMID, jac=True, args=(target, correctionMatrix), 
                            method='L-BFGS-B', bounds=[(0., float('inf'))]*len(initialMID),
                            options={'gtol': 1e-10, 'eps': 1e-08, 'maxiter': 15000, 'ftol': 2.220446049250313e-09, 
                                     'maxcor': 10, 'maxfun': 15000})
    return res

  def correctForNaturalAbundance(self, dataFrame, method="LSC"):
    '''
    Correct the Mass Isotope Distributions (MID) from a given dataFrame.
    Method: SMC (skewed Matrix correction) / LSC (Least Squares Skewed Correction)
    '''
    correctionMatrix = self.computeCorrectionMatrix(self.elementsDict, self.atomTracer, self.NaturalAbundanceDistributions, self.purityTracer)
    nRows, nCols = correctionMatrix.shape

    # ensure compatible sizes (will extend data)
    if nCols<dataFrame.shape[1]:
      print("The measure MID has more clusters than the correction matrix.")
    else:
      dfData = np.zeros((len(dataFrame), nCols))
      dfData[:dataFrame.shape[0], :dataFrame.shape[1]] = dataFrame.values

    if method == "SMC":
      # will mltiply the data by inverse of the correction matrix
      correctionMatrix = np.linalg.pinv(correctionMatrix)
      correctedData = np.matmul(dfData, correctionMatrix.transpose())
      # flatten unrealistic negative values to zero
      correctedData[correctedData<0] = 0

    elif method == "LSC":
      # Prepare multiprocessing optimization
      targetMIDList = dfData.tolist()
      initialMID = np.zeros_like(targetMIDList[0])
      argsList = [(self._computeCost, initialMID, targetMID, correctionMatrix) for targetMID in targetMIDList]

      # minimize for each MID
      allRes = [self._minimizeCost(args) for args in argsList]
      correctedData = np.vstack([res.x for res in allRes])

    return pd.DataFrame(columns=dataFrame.columns, data=correctedData[:, :dataFrame.shape[1]])



#############################################################################
# --------- DATA OBJECT CLASS ----------------------------------------------#
#############################################################################

class MSDataContainer:

  ##################
  ## Init and setup
  ##################

  def __init__(self, fileNames, internalRef="C19:0", tracer="H", tracerPurity=[0.00, 1.00]):
    assert len(fileNames)==2 , "You must choose 2 files!"
    self.internalRef = internalRef
    self._cholesterol = False
    self.tracer = tracer
    self.tracerPurity = tracerPurity
    self.NACMethod = "LSC" # least squares skewed matrix correction
    self.dataFileName, self.templateFileName = self.__getDataAndTemplateFileNames(fileNames)
    self._baseFileName = os.path.basename(self.dataFileName).split('.')[0]
    self.pathDirName = os.path.dirname(self.dataFileName)
    self.__regexExpression = {"Samples": '^(?!neg|S\d+$)',
                              "colNames": '(\d+)_(\d+)(?:\.\d+)?_(\d+)'}
    self.dataDf = self._computeFileAttributes()
    self.__standardDf_template = self.__getStandardsTemplateDf()
    self.volumeMixTotal = 500
    self.volumeMixForPrep = 100
    self.volumeStandards = [1, 5, 10, 20, 40, 80]

    self.standardDf_nMoles = self.computeStandardMoles()

    # for normalization
    # volume (uL) in which the original sample was diluted
    samplesLoc = self.dataDf.SampleName.str.match(self.__regexExpression["Samples"], na=False)
    self.numberOfSamples = len(self.dataDf.loc[samplesLoc])
    self.volumesOfDilution = [750]*self.numberOfSamples
    # volume (uL) of sample used in MS
    self.volumesOfSampleSoupUsed = [5]*self.numberOfSamples
    self.weightNormalization = False


  def __getDataAndTemplateFileNames(self, fileNames, templateKeyword="template"):
    '''Classify files (data or template) based on fileName'''
    dataFileName = [fileName for fileName in fileNames if templateKeyword not in fileName][0]
    templateFileName = [fileName for fileName in fileNames if fileName != dataFileName][0]
    return [dataFileName, templateFileName]

  def __parseIon(self, ion):
    ionID, ionMass, ionDescription = re.findall(self.__regexExpression["colNames"], ion)[0]
    return {"id": int(ionID), "mass": int(ionMass), "description": ionDescription}

  def __parseSampleColumns(self, columnNames):
    # indexed ions from columns
    ions = map(lambda name: self.__parseIon(name), columnNames)
    return list(ions)#[self.__getIndexedIon(ion, i) for i,ion in enumerate(ions)]

  def __isLabeledExperiment(self, ionsDetected):
    # if more than 40% of the ions are duplicate, it probably means that the file is 
    # from a labeled experimnets (lots of fragments for each ion)
    ionsList = list(map(lambda ion: ion["description"], ionsDetected))
    uniqueIons = set(ionsList)
    return len(uniqueIons)/len(ionsList) < 0.6

  def __getIonParentedGroups(self, ionsDetected):
  # groupby parental ions (save initial index for sorting later)
    groupedIons = groupby(enumerate(ionsDetected), key=lambda ion: ion[1]['description'])
    groupsIntraSorted = list(map(lambda group: (group[0], sorted(group[1], key=lambda ion: ion[1]['mass'])), groupedIons))

    # split groups if most abundant ion present
    finalGroups = []
    for key,group in groupsIntraSorted:
      # only process groups that have more than 1 ion
      if len(group) != 1:
        masses = np.array([ion[1]["mass"] for ion in group])
        differences = masses[1:]-masses[0:-1]
        idx = np.where(differences != 1)
        # and that have non unitary jumps in differences from ion to ion
        if len(idx[0])>0:
          start = 0
          for i in range(len(idx[0])+1):
            if i < len(idx[0]):
              end = idx[0][i]+1
              subgroup = group[start:end]
              start = idx[0][i] + 1
              finalGroups.append((f"{key}-{i}", subgroup))
            else:
              subgroup = group[start:]
              finalGroups.append((f"{key}-{i}", subgroup))
        else:
          finalGroups.append((key, group))
      else:
        finalGroups.append((key, group))
    return finalGroups


  def _computeFileAttributes(self):
    
    # extract columns
    columnsOfInterest = pd.read_excel(self.dataFileName, nrows=2).filter(regex=self.__regexExpression["colNames"]).columns
    # load data and template files and isolate data part
    df = pd.read_excel(self.dataFileName, skiprows=1)
    templateMap = pd.read_excel(self.templateFileName, sheet_name="MAP")

    # check if cholesterol experiment
    letter = df["Name"][0][0] # F or C
    df_Meta,df_Data = self.__getOrderedDfBasedOnTemplate(df, templateMap, letter)
    if letter == "C":
      self._cholesterol = True

    # assign columns names
    ionsDetected = self.__parseSampleColumns(columnsOfInterest)
    self.dataColNames = [f"C{ion['description'][:2]}:{ion['description'][2:]} ({ion['mass']})" for ion in ionsDetected]
    self.internalRefList = self.dataColNames
    self.experimentType = "Not Labeled"

    # Check if this is a labeled experiment.
    # If it is, need to rework the columns names by adding info of non parental ion
    if self.__isLabeledExperiment(ionsDetected):
      self.experimentType = "Labeled"

      # split groups if most abundant ion present
      finalGroups = self.__getIonParentedGroups(ionsDetected)
      
      if letter == "F":
        startM = 0
      else:
        assert len(finalGroups) == 2, "For cholesterol experiment we only expect 2 parental ions!"
        startM = -2

      sortedIonNames = [(idx, f"C{ion['description'][:2]}:{ion['description'][2:]} ({group[0][1]['mass']}) M.{n}") for (key,group) in finalGroups for n,(idx, ion) in enumerate(group)]
      orderedIdx,orderedIonNames = zip(*sortedIonNames)

      # reorder the columns by ions
      df_Data = df_Data.iloc[:, list(orderedIdx)]

      self.dataColNames = orderedIonNames
      # only parental ions for internalRefList
      self.internalRefList = [ f"C{carbon.split('-')[0][:2]}:{carbon.split('-')[0][2:]} ({group[0][1]['mass']})" for (carbon, group) in finalGroups]

    df_Data.columns = self.dataColNames

    # get sample meta info from template file
    df_TemplateInfo = self.__getExperimentMetaInfoFromMAP(templateMap)

    assert len(df_TemplateInfo)==len(df_Data), \
    f"The number of declared samples in the template (n={len(df_TemplateInfo)}) does not match the number of samples detected in the data file (n={len(df_Data)})"

    # save the number of columns (meta info) before the actual data
    self._dataStartIdx = len(df_Meta.columns)+len(df_TemplateInfo.columns)

    if (letter == "F") | (self.experimentType == "Not Labeled"):
      dataDf = pd.concat([df_Meta, df_TemplateInfo, df_Data.fillna(0)], axis=1)
    else:
      # if chol experiment, remove the M.-2 and M.-1
      dataDf = pd.concat([df_Meta, df_TemplateInfo, df_Data.iloc[:, 2:].fillna(0)], axis=1)
      # but save a copy with everything for posterity
      self.dataDf_chol = pd.concat([df_Meta, df_TemplateInfo, df_Data.fillna(0)], axis=1)
    return dataDf

  def __getOrderedDfBasedOnTemplate(self, df, templateMap, letter="F", skipCols=7):
    '''Get new df_Data and df_Meta based on template'''

    # reorder rows based on template and reindex with range
    newOrder = list(map(lambda x: f"{letter}{x.split('_')[1]}", templateMap.SampleID[templateMap.SampleName.dropna().index].values))[:len(df)]
    df.index=df["Name"]
    df = df.reindex(newOrder)
    df.index = list(range(len(df)))

    df_Meta = df[["Name", "Data File"]]
    df_Data = df.iloc[:, skipCols:] # 7 first cols are info
    return df_Meta, df_Data

  def __getExperimentMetaInfoFromMAP(self, templateMap):
    '''Return the meta info of the experiment'''
    # keep only rows with declared names
    declaredIdx = templateMap.SampleName.dropna().index
    templateMap = templateMap.loc[declaredIdx]
    templateMap.index = range(len(templateMap)) # in case there were missing values
    # fill in missing weights with 1
    templateMap.loc[templateMap.SampleWeight.isna(), "SampleWeight"]=1
    return templateMap[["SampleID", "SampleName", "SampleWeight", "Comments"]]

  def __getStandardsTemplateDf(self, sheetKeyword="STANDARD"):
    '''Loads the correct sheet for standard and returns it'''
    sheetName = f"{sheetKeyword}_{'_'.join(self.experimentType.upper().split(' '))}"
    templateStandard = pd.read_excel(self.templateFileName, sheet_name=sheetName)
    return templateStandard

  def __makeResultFolder(self):
    if self._cholesterol:
      suffix = "-CHOL"
    else:
      suffix = ""
    directory = f"{self.pathDirName}/results-{self._baseFileName}{suffix}"
    if not os.path.exists(directory):
      os.mkdir(directory)
    return directory

  ##################
  ## Analysis and Updates
  ##################

  def updateInternalRef(self, newInternalRef):
    '''Update FAMES chosen as internal reference and normalize data to it'''
    print(f"Internal Reference changed from {self.internalRef} to {newInternalRef}")
    self.internalRef = newInternalRef
    self.dataDf_norm = self.computeNormalizedData()

  def updateStandards(self, volumeMixForPrep, volumeMixTotal, volumeStandards):
    self.volumeMixForPrep = volumeMixForPrep
    self.volumeMixTotal = volumeMixTotal
    self.volumeStandards = volumeStandards
    self.standardDf_nMoles = self.computeStandardMoles()

  def computeStandardMoles(self):
    '''Calculate nMoles for the standards'''
    template = self.__standardDf_template.copy()
    template["Conc in Master Mix (ug/ul)"] = template["Stock conc (ug/ul)"]*template["Weight (%)"]/100*self.volumeMixForPrep/self.volumeMixTotal
    # concentration of each carbon per standard volume
    for ul in self.volumeStandards:
      template[f"Std-Conc-{ul}"]=ul*(template["Conc in Master Mix (ug/ul)"]+template["Extra"])
    # nMol of each FAMES per standard vol
    for ul in self.volumeStandards:
      template[f"Std-nMol-{ul}"] = 1000*template[f"Std-Conc-{ul}"]/template["MW"]
    # create a clean template with only masses and carbon name
    templateClean = pd.concat([template.Chain, template.filter(like="Std-nMol")], axis=1).transpose()
    templateClean.columns = [f"C{chain} ({int(mass)})" for chain,mass in zip(self.__standardDf_template.Chain, self.__standardDf_template.MW)]
    templateClean = templateClean.iloc[1:]
    return templateClean

  def getStandardAbsorbance(self):
    '''Get normalized absorbance data for standards'''
    matchedLocations = self.dataDf_norm.SampleName.str.match('S[0-9]+', na=False)
    return self.dataDf_norm.loc[matchedLocations]

  def updateTracer(self, newTracer):
    self.tracer = newTracer
    print(f"The tracer has been updated to {newTracer}")
    self.computeNACorrectionDf()

  def updateTracerPurity(self, newPurity):
    self.tracerPurity = newPurity
    self.computeNACorrectionDf()

  def updateNACMethod(self, newMethod):
    self.NACMethod = newMethod
    print(f"The correction method for natural abundance has been updated to {newMethod}")
    self.computeNACorrectionDf()

  def updateVolumesOfSampleDilution(self, newVolumeOfDilution, newVolumeOfSampleUsed, useValueDilution=True, useValueSample=True):
    if useValueDilution:
      self.volumesOfDilution = [newVolumeOfDilution]*self.numberOfSamples
    if useValueSample:
      self.volumesOfSampleSoupUsed = [newVolumeOfSampleUsed]*self.numberOfSamples
    print(f"The volumes used for normalization have been updated:\n\tVolume of dilution: {self.volumesOfDilution}\n\tVolume of sample used: {self.volumesOfSampleSoupUsed}")

  def updateVolumeOfDilutionFromTemplateFile(self, columnName, activated, variable="dilution", backupValueDilution=750, backupValueSample=5, useBackupDilution=True, useBackupSample=True):
    templateMap = pd.read_excel(self.templateFileName, sheet_name="MAP")
    declaredIdx = templateMap.SampleName.dropna()[templateMap.SampleName.dropna().str.match(self.__regexExpression["Samples"], na=False)].index
    if ((variable == "dilution") & (activated)):
      self.volumesOfDilution = templateMap.loc[declaredIdx, columnName].values
      print(f"The dilution volumes used for normalization have been updated from template to {self.volumesOfDilution}")
      assert len(self.volumesOfDilution[~np.isnan(self.volumesOfDilution)])==len(declaredIdx),\
      f"The number of volume of dilutions declared in the Template file (n={len(self.volumesOfDilution[~np.isnan(self.volumesOfDilution)])}) is different than the number of samples declared (n={len(declaredIdx)})"
    elif ((variable == "sample") & (activated)):
      self.volumesOfSampleSoupUsed = templateMap.loc[declaredIdx, columnName].values
      print(f"The sample volumes used for normalization have been updated from template to {self.volumesOfSampleSoupUsed}")
      assert len(self.volumesOfSampleSoupUsed[~np.isnan(self.volumesOfSampleSoupUsed)])==len(declaredIdx),\
      f"The number of sample volumes declared in the Template file (n={len(self.volumesOfSampleSoupUsed[~np.isnan(self.volumesOfSampleSoupUsed)])}) is different than the number of samples declared (n={len(declaredIdx)})"
    else:
      self.updateVolumesOfSampleDilution(backupValueDilution, backupValueSample, useBackupDilution, useBackupSample)

  def updateNormalizationType(self, newType):
    self.weightNormalization = bool(newType)
    if (self.weightNormalization):
      typeNorm = "by total weight"
    else:
      typeNorm = "by relative weight"
    print(f"The normalization when computing the data has been changed to '{typeNorm}'")

  # Debouncing active for computing the NA correction.
  # Makes for a smoother user experience (no lagging) when ion purity are changed int he textbox
  # Note that this means that the function will be called with the specified delay after parameters are changed
  @debounce(1.1) 
  def computeNACorrectionDf(self):
    self.dataDf_corrected = self.correctForNaturalAbundance()
    self.dataDf_labeledProportions = self.calculateLabeledProportionForAll()

  def computeNormalizedData(self):
    '''Normalize the data to the internal ref'''
    if self.experimentType == "Not Labeled":
      dataDf_norm = self.dataDf.copy()
      dataDf_norm.iloc[:, self._dataStartIdx:] = dataDf_norm.iloc[:, self._dataStartIdx:].divide(dataDf_norm[self.internalRef], axis=0)
    else:
      sumFracDf = self.calculateSumIonsForAll()
      sumFracDf = sumFracDf.divide(sumFracDf[self.internalRef], axis=0)
      # sumFracDf = pd.DataFrame(columns=sumFracDf.columns, data=sumFracDf.values/sumFracDf[self.internalRef].values[:, np.newaxis])
      dataDf_norm = pd.concat([self.dataDf.iloc[:, :self._dataStartIdx], sumFracDf], axis=1)
    return dataDf_norm

  def correctForNaturalAbundance(self):
    '''Correct all the data for natural abundance'''
    correctedData = self.dataDf.iloc[:, :self._dataStartIdx]
    for parentalIon in self.internalRefList:
      ionMID = self.dataDf.filter(like=parentalIon)
      if ionMID.shape[1]<=1:
        # no clusters, go to next
        print(parentalIon, "doesn't have non parental ions")
        correctedData = pd.concat([correctedData, ionMID], axis=1)
        continue
      ionNA = NAProcess(parentalIon, self.tracer, purityTracer=self.tracerPurity, CHOL=self._cholesterol)
      correctedIonData = ionNA.correctForNaturalAbundance(ionMID, method=self.NACMethod)
      correctedData = pd.concat([correctedData, correctedIonData], axis=1)

    print(f"The MIDs have been corrected using the {self.NACMethod} method (tracer: {self.tracer}, purity: {self.tracerPurity})")
    return correctedData

  def calculateSumIonsForAll(self):
    '''Return df of the summed fractions for all the ions'''
    if self._cholesterol:
      dataToSum = self.dataDf_chol
    else:
      dataToSum = self.dataDf
    sumFrac = pd.concat([dataToSum.filter(like=parentalIon).sum(axis=1) for parentalIon in self.internalRefList], axis=1)
    sumFrac.columns = self.internalRefList
    return sumFrac

  def calculateLabeledProportion(self, df):
    '''Calculate the proportion of labeling in non-parental ions (M.0 must be first column).'''
    total = df.sum(axis=1)
    return (total - df.iloc[:,0])/total

  def calculateLabeledProportionForAll(self):
    '''Return dataFrame of the labeling proportions for all the ions'''
    proportions = pd.concat([self.calculateLabeledProportion(self.dataDf_corrected.filter(like=parentalIon)) for parentalIon in self.internalRefList], axis=1)
    proportions.columns = self.internalRefList
    return pd.concat([self.dataDf.iloc[:, :self._dataStartIdx], proportions], axis=1)

  def saveStandardCurvesAndResults(self, useMask=False):
    '''Save figures and all the relevant data'''

    # get current folder and create result folder if needed
    savePath = self.__makeResultFolder()

    if not useMask:
      extension = ""
    else:
      extension = "_modified"

    self.dataDf_quantification = self.computeQuantificationFromStandardFits(useMask=useMask)
    # index of data of interest (not the standards)
    expDataLoc = self.dataDf_quantification.SampleName.str.match(self.__regexExpression["Samples"], na=False)
    stdAbsorbance = self.getStandardAbsorbance().iloc[:, self._dataStartIdx:]
    quantificationDf = self.dataDf_quantification.iloc[:, 3:]

    nTotal = len(quantificationDf.columns)
    # grid of plots
    nCols = 4
    if nTotal%4==0:
      nRows = int(nTotal/nCols)
    else:
      nRows = int(np.floor(nTotal/nCols)+1)

    # fig1 (only standards) and fig2 (standards + calculated FAMES concentration)
    fig1,axes1 = plt.subplots(ncols=nCols, nrows=nRows, figsize=(20, nRows*4), constrained_layout=True)
    fig2,axes2 = plt.subplots(ncols=nCols, nrows=nRows, figsize=(20, nRows*4), constrained_layout=True)

    fig1.suptitle(f"Experiment: {self._baseFileName}")
    fig2.suptitle(f"Experiment: {self._baseFileName}")

    for i,(col,ax1,ax2) in enumerate(zip(quantificationDf.columns,  axes1.flatten(), axes2.flatten())):
      # if slope/intercept from standard are present for this ion, then continue
      slope,intercept,r2 = self.standardDf_fitResults.loc[["slope", "intercept", "R2"], col]
      
      # standards values and fits
      try:
        xvals = self.standardDf_nMoles[col].values
      except:
        # if error, it means that parental ion data were used
        parentalIon = self._checkIfParentalIonDataExistsFor(col)[1]
        xvals = self.standardDf_nMoles[parentalIon].values
      yvals = stdAbsorbance[col].values
      try:
        mask = self._maskFAMES[col]["newMask"]
      except:
        mask = self._maskFAMES[col]["originalMask"]
      xfit = [np.min(xvals), np.max(xvals)]
      yfit = np.polyval([slope, intercept], xfit)
      
      # Fig 1 
      ax1.plot(xvals[mask], yvals[mask], "o", color="#00BFFF")
      ax1.plot(xvals[[not i for i in mask]], yvals[[not i for i in mask]], "o", mfc="none", color="black", mew=2)
      ax1.plot(xfit, yfit, "-", color="#fb4c52")
      ax1.text(ax1.get_xlim()[0]+(ax1.get_xlim()[1]-ax1.get_xlim()[0])*0.05, ax1.get_ylim()[0]+(ax1.get_ylim()[1]-ax1.get_ylim()[0])*0.9, f"R2={r2:.4f}", size=14, color="#ce4ad0")
      ax1.text(ax1.get_xlim()[0]+(ax1.get_xlim()[1]-ax1.get_xlim()[0])*0.97, ax1.get_ylim()[0]+(ax1.get_ylim()[1]-ax1.get_ylim()[0])*0.05, f"y={slope:.4f}x+{intercept:.4f}", size=14, color="#fb4c52", ha="right")
      ax1.set_title(col)
      ax1.set_xlabel("Quantity (nMoles)")
      ax1.set_ylabel("Absorbance")

      # Fig 2
      ax2.plot(xvals[mask], yvals[mask], "o", color="#00BFFF")
      ax2.plot(xvals[[not i for i in mask]], yvals[[not i for i in mask]], "x", color="black", ms=3)
      ax2.plot(xfit, yfit, "-", color="#fb4c52")
      # add values calculated from curve (visually adjust for normalization by weight done above)
      ax2.plot(quantificationDf.loc[expDataLoc, col], self.dataDf_norm.loc[expDataLoc, col], "o", color="#FF8B22", alpha=0.3)
      ax2.text(ax2.get_xlim()[0]+(ax2.get_xlim()[1]-ax2.get_xlim()[0])*0.05, ax2.get_ylim()[0]+(ax2.get_ylim()[1]-ax2.get_ylim()[0])*0.9, f"R2={r2:.4f}", size=14, color="#ce4ad0")
      ax2.text(ax2.get_xlim()[0]+(ax2.get_xlim()[1]-ax2.get_xlim()[0])*0.97, ax2.get_ylim()[0]+(ax2.get_ylim()[1]-ax2.get_ylim()[0])*0.05, f"y={slope:.4f}x+{intercept:.4f}", size=14, color="#fb4c52", ha="right")
      ax2.set_title(col)
      ax2.set_xlabel("Quantity (nMoles)")
      ax2.set_ylabel("Absorbance")

    #####################
    # Save data and figures

    # save figures
    fig1.savefig(f"{savePath}/standard-fit{extension}.pdf")
    fig2.savefig(f"{savePath}/standard-fit-with-data{extension}.pdf")
    # close Matplotlib processes
    plt.close('all')

    # Create a Pandas Excel writer using XlsxWriter as the engine.
    if self._cholesterol:
      suffix = "-CHOL"
    else:
      suffix = ""
    writer = pd.ExcelWriter(f"{savePath}/results-{self._baseFileName}{suffix}{extension}.xlsx", engine='xlsxwriter')

    normalization = self.getNormalizationArray()
    # Write each dataframe to a different worksheet.
    # standards
    standards = self.getConcatenatedStandardResults()
    standards.to_excel(writer, sheet_name='Standards', index=True)
    # data
    self.dataDf_quantification.loc[expDataLoc].to_excel(writer, sheet_name='QuantTotal_nMoles', index=False)
    resNorm = pd.concat([self.dataDf_norm["SampleID"], self.dataDf_norm["SampleName"], self.dataDf_norm["Comments"], quantificationDf.divide(normalization, axis=0)], axis=1)
    resNorm.loc[expDataLoc].to_excel(writer, sheet_name='QuantTotal_nMoles_mg', index=False)
    if self.experimentType == "Labeled":
      newlySynthetizedMoles = quantificationDf*self.dataDf_labeledProportions[quantificationDf.columns]
      res_newlySynthetizedMoles = pd.concat([self.dataDf_norm["SampleID"], self.dataDf_norm["SampleName"], self.dataDf_norm["Comments"], newlySynthetizedMoles], axis=1)
      # uL of liver soup used = 5uL (the initial liver was diluted in 750)
      res_newlySynthetizedMoles_norm = pd.concat([self.dataDf_norm["SampleID"], self.dataDf_norm["SampleName"], self.dataDf_norm["Comments"], newlySynthetizedMoles.divide(normalization, axis=0)], axis=1)
      res_newlySynthetizedMoles.loc[expDataLoc].to_excel(writer, sheet_name='QuantSynthetized_nMoles', index=False)
      res_newlySynthetizedMoles_norm.loc[expDataLoc].to_excel(writer, sheet_name='QuantSynthetized_nMoles_mg', index=False)
      labeledProp = self.dataDf_labeledProportions[["SampleID", "SampleName", "Comments", *self.dataDf_labeledProportions.columns[self._dataStartIdx:]]]
      labeledProp.loc[expDataLoc].to_excel(writer, sheet_name='PercentageSynthetized', index=False)
    if (self._cholesterol) & (self.experimentType=="Labeled"):
      originalData = self.dataDf_chol
    else:
      originalData = self.dataDf
    originalData.to_excel(writer, sheet_name='OriginalData', index=False)
    self.dataDf_norm.to_excel(writer, sheet_name='OriginalData_normToInternalRef', index=False)
  
    # add a sheet with experiment log
    nSamples = len(self.volumesOfDilution)
    nVolumesStandards = len(self.volumeStandards)
    # keep the lengthiest to use to extend other arrays
    nMax = nSamples if (nSamples >= nVolumesStandards) else nVolumesStandards
    log = {
      "Experiment type": [self.experimentType] + [np.nan]*(nMax-1),
      "Volume Mix Total": [self.volumeMixTotal] + [np.nan]*(nMax-1),
      "Volume Mix Used": [self.volumeMixForPrep] + [np.nan]*(nMax-1),
      "Internal Reference": [self.internalRef] + [np.nan]*(nMax-1),
      "Volume standards": list(self.volumeStandards) + [np.nan]*(nMax-len(self.volumeStandards)),
      "Volume of Dilution": list(self.volumesOfDilution) + [np.nan]*(nMax-len(self.volumesOfDilution)),
      "Volume of Sample Measured": list(self.volumesOfSampleSoupUsed) + [np.nan]*(nMax-len(self.volumesOfSampleSoupUsed)),
      "Normalization": ["Weigth only" if self.weightNormalization else "Relative Weight"] + [np.nan]*(nMax-1),
      "Isotope tracer": [self.tracer] + [np.nan]*(nMax-1),
      "Isotope tracer purity": list(self.tracerPurity) + [np.nan]*(nMax-len(self.tracerPurity))
    }
    pd.DataFrame(log).transpose().to_excel(writer, sheet_name='Log', index=True)

    # Close the Pandas Excel writer and output the Excel file.
    writer.save()
    
    print(f"The standard curves have been saved at {savePath}/standard-fit{extension}.pdf")
    print(f"The results calculated from the standard regression lines have been saved at {savePath}/standard-fit-with-data{extension}.pdf")
    print(f"The analysis results have been saved at {savePath}/results{extension}.xls")

  def getNormalizationArray(self):
    '''Return an index-aware normalization factor for all the samples'''
    normalization = self.dataDf_norm["SampleWeight"]
    if not self.weightNormalization:
      # if not by weight only
      normalization = normalization.replace(normalization, 1)
      samplesLoc = self.dataDf.SampleName.str.match(self.__regexExpression["Samples"], na=False)
      samplesWeights = self.dataDf_norm["SampleWeight"].loc[samplesLoc]
      normalization.loc[samplesLoc] = self.volumesOfSampleSoupUsed*samplesWeights/(self.volumesOfDilution+samplesWeights)
    return normalization

  def computeStandardFits(self, useMask=False):
    ''' Return a dataFrame of the slope/intercept for all the valid standards'''
    
    # will store final results
    fitDf = pd.DataFrame(index=["slope", "intercept", "R2"])

    stdAbsorbance = self.getStandardAbsorbance().iloc[:, self._dataStartIdx:]
    assert len(stdAbsorbance) == len(self.standardDf_nMoles),\
    f"The number of standards entered (n={len(self.standardDf_nMoles)}) is different than the number of standards declared in the data file (n={len(stdAbsorbance)})"

    if not useMask:
      self._maskFAMES = {}

    for i,col in enumerate(stdAbsorbance.columns):
      if col in self.standardDf_nMoles.columns:
        # if nMoles data are present for this exact ion
        xvals = self.standardDf_nMoles[col].values
      else:
        isParentalIon = self._checkIfParentalIonDataExistsFor(col)
        if isParentalIon[0]:
          # if there is a matching parental ion, then use the nMoles data from it
          parentalIon = isParentalIon[1]
          xvals = self.standardDf_nMoles[parentalIon].values
          print(f"Standard data for {col} were missing but parental ion {parentalIon} data were used for the fit")
        else:
          # no match, skip this ion
          print(f"No standard data were found for {col}, no quantification possible for it.")
          continue

      yvals = stdAbsorbance[col].values

      if not useMask:
        mask1 = [~np.logical_or(np.isnan(x), np.isnan(y)) for x,y in zip(xvals, yvals)]
        mask2 = [~np.logical_or(np.isnan(x), y==0) for x,y in zip(xvals, yvals)]
        mask = [(m1 & m2) for m1,m2 in zip(mask1, mask2)]
        # add carbon to valid standard FAMES and save mask
        self._maskFAMES[col] = {"originalMask": mask}
      else:
        try:
          # were the points used for the fit modified and a new mask created for this ion?
          mask = self._maskFAMES[col]["newMask"]
        except:
          mask = self._maskFAMES[col]["originalMask"]

      xvalsToFit =  np.array(xvals[mask], dtype=float)
      yvalsToFit = np.array(yvals[mask], dtype=float)
      if ((len(xvalsToFit)<3)|len(yvalsToFit)<3):
        print(f"Standard fit of {col} skipped (not enough values)")
        continue
      # fitDf[col] = np.polyfit(xvalsToFit, yvalsToFit, 1)
      slope,intercept,rvalue,pvalue,stderr = stats.linregress(xvalsToFit, yvalsToFit)
      fitDf[col] = [slope, intercept, rvalue**2]
    
    # save fits in object
    self.standardDf_fitResults = fitDf

    return fitDf

  def _checkIfParentalIonDataExistsFor(self, ion):
    '''For a given ion, will return the corresponding parental ion if it exists'''
    try:
      # extract carbon and mass of ion
      carbon,mass = re.match("(C[0-9]+:[0-9]+) \(([0-9]+)\)", ion).groups()
      mass = int(mass)
      # check if a similar carbon is present in standard data we have
      boolIdx = self.standardDf_nMoles.columns.str.match(f"{carbon}")
      matchingCarbons = self.standardDf_nMoles.columns[boolIdx]
      if len(matchingCarbons)>0:
        # get mass all matching carbons and only consider the heaviest one as the parental ion
        massOfMatchingCarbons = [[i,int(mass)] for i,ion in enumerate(matchingCarbons) for mass in re.match(f"{carbon} \(([0-9]+)\)", ion).groups()]
        idxHeaviest,massHeaviest = sorted(massOfMatchingCarbons, key = lambda entry: entry[1])[-1]
        return [True, matchingCarbons[idxHeaviest]]
      else:
        return [False]
    except:
      return [False]

  def computeQuantificationFromStandardFits(self, useMask=False):
    '''Use fits (slope/intercept) of standards to quantify FAMES from absorbance'''
    standardFits = self.computeStandardFits(useMask=useMask)
    # will store final results
    resultsDf = pd.DataFrame(index=self.dataDf_norm.index)

    for i,col in enumerate(standardFits.columns):
      slope,intercept = standardFits.loc[["slope", "intercept"], col]
      resultsDf[col] = ((self.dataDf_norm[col]-intercept)/slope)

    return pd.concat([self.dataDf_norm["SampleID"], self.dataDf_norm["SampleName"], self.dataDf_norm["Comments"], resultsDf], axis=1)

  def getConcatenatedStandardResults(self):
    '''Return a formatted dataFrame with slop/intercept and nMoles for each standard'''
    return pd.concat([self.standardDf_fitResults, self.standardDf_nMoles], axis=0, sort=True)


#############################################################################
# --------- GRAPHICAL USER INTERFACE ---------------------------------------#
#############################################################################

def initialFileChoser(directory=False):
  '''Temporary app window to get filenames before building main app'''
  if not directory:
    directory = os.getcwd()
  # Build a list of tuples for each file type the file dialog should display
  appFiletypes = [('excel files', '.xlsx'), ('all files', '.*')]
  # Main window
  appWindow = tk.Tk()
  appWindow.geometry("0x0") # hide the window
  appTitle = appWindow.title("Choose Files")
  # Ask the user to select a one or more file names.
  fileNames = filedialog.askopenfilenames(parent=appWindow,
                                          initialdir=directory,
                                          title="Please select the files:",
                                          filetypes=appFiletypes
                                          )
  appWindow.destroy() # close the app
  return fileNames


# Text widget that can call a callback function when modified
# see https://stackoverflow.com/questions/40617515/python-tkinter-text-modified-callback
class CustomText(tk.Text):
  def __init__(self, *args, **kwargs):
    """A text widget that report on internal widget commands"""
    tk.Text.__init__(self, *args, **kwargs)

    # create a proxy for the underlying widget
    self._orig = self._w + "_orig"
    self.tk.call("rename", self._w, self._orig)
    self.tk.createcommand(self._w, self._proxy)

  def _proxy(self, command, *args):
    cmd = (self._orig, command) + args
    result = self.tk.call(cmd)

    if command in ("insert", "delete", "replace"):
        self.event_generate("<<TextModified>>")

    return result


class MSAnalyzer:
  def __init__(self, dataObject):
    self.window = tk.Tk()
    self.window.title("MS Analyzer")
    self.dataObject = dataObject
    self.FANames = dataObject.internalRefList#.dataColNames
    self.internalRef = dataObject.internalRef
    self.create_widgets()

  def create_widgets(self):
    # Create some room around all the internal frames
    self.window['padx'] = 5
    self.window['pady'] = 5

    # - - - - - - - - - - - - - - - - - - - - -
    # The FAMES frame (for internal control)
    FAMESframe = ttk.LabelFrame(self.window, text="Select the internal control", relief=tk.GROOVE)
    FAMESframe.grid(row=1, column=1, columnspan=3, sticky=tk.E + tk.W + tk.N + tk.S, padx=2)

    FAMESListLabel = tk.Label(FAMESframe, text="LIPID", fg="black", bg="#ECECEC")
    FAMESListLabel.grid(row=2, column=1, sticky=tk.W + tk.N)

    # by default, choose internal reference defined in dataObject (C19:0)
    try:
      idxInternalRef = [i for i,name in enumerate(self.FANames) if self.dataObject.internalRef in name][0]
    except:
      # it was probably a cholesterol file, take last ion as internal reference by default
      idxInternalRef = len(self.FANames)-1

    self.FAMESLabelCurrent = tk.Label(FAMESframe, text=f"The current internal control is {self.FANames[idxInternalRef]}", fg="white", bg="black")
    self.FAMESLabelCurrent.grid(row=3, column=1, columnspan=3)

    self.FAMESListValue = tk.StringVar()
    self.FAMESListValue.trace('w', lambda index,value,op : self.__updateInternalRef(FAMESList.get()))
    FAMESList = ttk.Combobox(FAMESframe, height=6, textvariable=self.FAMESListValue, state="readonly", takefocus=False)
    FAMESList.grid(row=2, column=2, columnspan=2)
    FAMESList['values'] = self.FANames
    FAMESList.current(idxInternalRef)

    # - - - - - - - - - - - - - - - - - - - - -
    # The standards frame (for fitting)
    Standardframe = ttk.LabelFrame(self.window, text="Standards", relief=tk.RIDGE)
    Standardframe.grid(row=4, column=1, columnspan=3, sticky=tk.E + tk.W + tk.N + tk.S, padx=2, pady=6)
    
    # variables declaration
    self.volTotalVar = tk.IntVar()
    self.volMixVar = tk.IntVar()
    self.stdVols = self.dataObject.volumeStandards

    self.volTotalVar.set(self.dataObject.volumeMixTotal)
    self.volMixVar.set(self.dataObject.volumeMixForPrep)

    # Vol mix total
    self.volTotalVar.trace('w', lambda index,value,op : self.__updateVolumeMixTotal(self.volTotalVar.get()))
    volTotalSpinbox = tk.Spinbox(Standardframe, from_=0, to=1000, width=5, textvariable=self.volTotalVar, command= lambda: self.__updateVolumeMixTotal(self.volTotalVar.get()), justify=tk.RIGHT)
    volTotalSpinbox.grid(row=5, column=2, sticky=tk.W, pady=3)
    volTotalLabel = tk.Label(Standardframe, text="Vol. Mix Total", fg="black", bg="#ECECEC")
    volTotalLabel.grid(row=5, column=1, sticky=tk.W)

    # Vol mix
    self.volMixVar.trace('w', lambda index,value,op : self.__updateVolumeMixForPrep(self.volMixVar.get()))
    volMixSpinbox = tk.Spinbox(Standardframe, from_=0, to=1000, width=5, textvariable=self.volMixVar, command= lambda: self.__updateVolumeMixForPrep(self.volMixVar.get()), justify=tk.RIGHT)
    volMixSpinbox.grid(row=6, column=2, sticky=tk.W, pady=3)
    volMixLabel = tk.Label(Standardframe, text="Vol. Mix", fg="black", bg="#ECECEC")
    volMixLabel.grid(row=6, column=1, sticky=tk.W)

    # Standards uL
    StandardVols = CustomText(Standardframe, height=7, width=15)
    StandardVols.grid(row=5, rowspan=3, column=3, padx=20)
    StandardVols.insert(tk.END, "Standards (ul)\n"+"".join([f"{vol}\n" for vol in self.stdVols]))
    StandardVols.bind("<<TextModified>>", self.__updateVolumeStandards)

    # - - - - - - - - - - - - - - - - - - - - -
    # Actions frame
    Actionframe = ttk.LabelFrame(self.window, text="Actions", relief=tk.RIDGE)
    Actionframe.grid(row=1, column=4, columnspan=1, sticky=tk.E + tk.W + tk.N + tk.S, padx=2)
    
    # Quit button in the upper right corner
    quit_button = ttk.Button(Actionframe, text="Quit", command=lambda: self.quitApp(self.window))
    quit_button.grid(row=1, column=1)

    # Compute Results button
    ttk.Style().configure("multiLine.TButton", justify=tk.CENTER)
    computeResultsButton = ttk.Button(Actionframe, style="multiLine.TButton", text="Compute\nresults", command=lambda: self.computeResults())
    computeResultsButton.grid(row=2, column=1, pady=5)

    # - - - - - - - - - - - - - - - - - - - - -
    # The normalization frame
    Normalizationframe = ttk.LabelFrame(self.window, text="Normalization", relief=tk.RIDGE)
    Normalizationframe.grid(row=9, column=1, columnspan=3, sticky=tk.E + tk.W + tk.N + tk.S, padx=2, pady=6)
    
    self.weightNormalizationOnlyVar = tk.IntVar()
    self.weightNormalizationOnlyVar.set(self.dataObject.weightNormalization)
    self.weightNormalizationOnlyVar.trace('w', lambda index,value,op : self.dataObject.updateNormalizationType(self.weightNormalizationOnlyVar.get()))
    checkbuttonVolOfDilution = ttk.Checkbutton(Normalizationframe, text="Normalize by weight only", variable=self.weightNormalizationOnlyVar)#, command=lambda: self.dataObject.updateNormalizationType(self.weightNormalizationOnlyVar.get()))
    checkbuttonVolOfDilution.grid(row=10, column=1, columnspan=2)

    # variables declaration
    self.volOfDilutionVar = tk.IntVar()
    self.volOfSampleUsedVar = tk.IntVar()
    self.useVolOfDilutionVar = tk.IntVar()
    self.useVolOfSampleUsedVar = tk.IntVar()

    self.volOfDilutionVar.set(self.dataObject.volumesOfDilution[0])
    self.volOfSampleUsedVar.set(self.dataObject.volumesOfSampleSoupUsed[0])
    self.useVolOfDilutionVar.set(False)
    self.useVolOfSampleUsedVar.set(False)

    # Vol of dilution total
    self.volOfDilutionVar.trace('w', lambda index,value,op : self.__updateVolumesForNormalization(self.volOfDilutionVar.get(), self.volOfSampleUsedVar.get(), not self.useVolOfDilutionVar.get(), not self.useVolOfSampleUsedVar.get()))
    volOfDilutionLabel = tk.Label(Normalizationframe, text="Vol. Dilution", fg="black", bg="#ECECEC")
    volOfDilutionLabel.grid(row=11, column=1, sticky=tk.W)
    volOfDilutionSpinbox = tk.Spinbox(Normalizationframe, from_=0, to=1000, width=5, textvariable=self.volOfDilutionVar, command= lambda: self.__updateVolumesForNormalization(self.volOfDilutionVar.get(), self.volOfSampleUsedVar.get(), not self.useVolOfDilutionVar.get(), not self.useVolOfSampleUsedVar.get()), justify=tk.RIGHT)
    volOfDilutionSpinbox.grid(row=11, column=2, sticky=tk.W, pady=3)

    checkbuttonVolOfDilution = ttk.Checkbutton(Normalizationframe, text="Use volumes from template", variable=self.useVolOfDilutionVar, 
      command=lambda: self.dataObject.updateVolumeOfDilutionFromTemplateFile(
        "VolumeOfDilution", 
        self.useVolOfDilutionVar.get(),
        variable="dilution", 
        backupValueDilution=self.volOfDilutionVar.get(),
        backupValueSample=self.volOfSampleUsedVar.get(),
        useBackupDilution=not self.useVolOfDilutionVar.get(), 
        useBackupSample=not self.useVolOfSampleUsedVar.get()))
    checkbuttonVolOfDilution.grid(row=11, column=3)

    # Vol of sample used
    self.volOfSampleUsedVar.trace('w', lambda index,value,op : self.__updateVolumesForNormalization(self.volOfDilutionVar.get(), self.volOfSampleUsedVar.get(), not self.useVolOfDilutionVar.get(), not self.useVolOfSampleUsedVar.get()))
    volOfSampleUsedLabel = tk.Label(Normalizationframe, text="Vol. Sample", fg="black", bg="#ECECEC")
    volOfSampleUsedLabel.grid(row=12, column=1, sticky=tk.W)
    volOfSampleUsedSpinbox = tk.Spinbox(Normalizationframe, from_=0, to=1000, width=5, textvariable=self.volOfSampleUsedVar, command= lambda: self.__updateVolumesForNormalization(self.volOfDilutionVar.get(), self.volOfSampleUsedVar.get(), not self.useVolOfDilutionVar.get(), not self.useVolOfSampleUsedVar.get()), justify=tk.RIGHT)
    volOfSampleUsedSpinbox.grid(row=12, column=2, sticky=tk.W, pady=3)

    checkbuttonVolOfSampleUsed = ttk.Checkbutton(Normalizationframe, text="Use volumes from template", variable=self.useVolOfSampleUsedVar, 
      command=lambda: self.dataObject.updateVolumeOfDilutionFromTemplateFile(
        "VolumeOfSampleUsed", 
        self.useVolOfSampleUsedVar.get(),
        variable="sample", 
        backupValueDilution=self.volOfDilutionVar.get(),
        backupValueSample=self.volOfSampleUsedVar.get(),
        useBackupDilution=not self.useVolOfDilutionVar.get(), 
        useBackupSample=not self.useVolOfSampleUsedVar.get()))
    checkbuttonVolOfSampleUsed.grid(row=12, column=3)

    if self.dataObject.experimentType == "Labeled":
      # - - - - - - - - - - - - - - - - - - - - -
      # The Natural Abundance Correction frame 
      Correctionframe = ttk.LabelFrame(self.window, text="Natural Abundance Correction", relief=tk.RIDGE)
      Correctionframe.grid(row=13, column=1, columnspan=3, sticky=tk.E + tk.W + tk.N + tk.S, padx=2, pady=6)

      # Natural Abundance Correction Method
      NACLabel = tk.Label(Correctionframe, text="Method:", fg="black", bg="#ECECEC")
      NACLabel.grid(row=14, column=1, columnspan=2, sticky=tk.W)
      self.radioCorrectionMethodVariable = tk.StringVar()
      self.radioCorrectionMethodVariable.set("LSC")
      self.radioCorrectionMethodVariable.trace('w', lambda index,value,op : self.__updateNACorrectionMethod(self.radioCorrectionMethodVariable.get()))
      methodButton1 = ttk.Radiobutton(Correctionframe, text="Least Squares Skewed Matrix",
                                     variable=self.radioCorrectionMethodVariable, value="LSC")
      methodButton2 = ttk.Radiobutton(Correctionframe, text="Skewed Matrix",
                                     variable=self.radioCorrectionMethodVariable, value="SMC")
      methodButton1.grid(row=15, column=1, columnspan=2, sticky=tk.W)
      methodButton2.grid(row=16, column=1 , columnspan=2, sticky=tk.W)

      # isotope tracer
      TracerLabel = tk.Label(Correctionframe, text="Atom tracer:", fg="black", bg="#ECECEC")
      TracerLabel.grid(row=13, column=3, sticky=tk.E)
      self.TracerListValue = tk.StringVar()
      self.TracerListValue.trace('w', lambda index,value,op : self.__updateTracer(TracerList.get()))
      TracerList = ttk.Combobox(Correctionframe, textvariable=self.TracerListValue, width=3, state="readonly", takefocus=False)
      TracerList.grid(row=14, column=3, sticky=tk.E)
      TracerList['values'] = ["C", "H", "O"]
      TracerList.current([i for i,tracer in enumerate(TracerList['values']) if tracer==self.dataObject.tracer][0])

      # Standards uL
      self.tracerPurity = self.dataObject.tracerPurity
      TracerPurity = CustomText(Correctionframe, height=3, width=12)
      TracerPurity.grid(row=15, column=3, sticky=tk.E)
      TracerPurity.insert(tk.END, "Purity:\n"+" ".join([f"{pur}" for pur in self.tracerPurity]))
      TracerPurity.bind("<<TextModified>>", self.__updateTracerPurity)

      # Compute Results button
      inspectCorrectionButton = ttk.Button(Correctionframe, text="Inspect NA correction", command=lambda: self.inspectCorrectionPlots())
      inspectCorrectionButton.grid(row=16, column=2, columnspan=2, pady=5)
  
  def quitApp(self, window):
    # close Matplotlib processes if any
    plt.close('all')
    window.destroy()

  def popupMsg(self, msg):
    '''Popup message window'''
    popup = tk.Tk()
    popup.wm_title("Look at the standard plots!")
    label = ttk.Label(popup, text=msg, font=("Verdana", 14))
    label.grid(row=1, column=1, columnspan=2, padx=10, pady=10)
      
    B1 = ttk.Button(popup, text="Yes", command = lambda: self.inspectStandardPlots(popup))
    B1.grid(row=2, column=1, pady=10)

    def quitApp():
      popup.destroy()
      app.window.destroy()

    B2 = ttk.Button(popup, text="No", command = quitApp)
    B2.grid(row=2, column=2, pady=10)
    popup.mainloop()

  def __updateInternalRef(self, newInternalRef):
    '''Update FAMES chosen as internal reference'''
    self.dataObject.updateInternalRef(newInternalRef)
    self.FAMESLabelCurrent.config(text=f"The current internal control is {newInternalRef}")

  def __updateVolumeMixTotal(self, newVolumeMixTotal):
    self.dataObject.updateStandards(self.volMixVar.get(), newVolumeMixTotal, self.stdVols)
    print(f"The volumeMixTotal has been updated to {newVolumeMixTotal}")

  def __updateVolumeMixForPrep(self, newVolumeMixForPrep):
    self.dataObject.updateStandards(newVolumeMixForPrep, self.volTotalVar.get(), self.stdVols)
    print(f"The volumeMixForPrep has been updated to {newVolumeMixForPrep}")

  def __updateVolumeStandards(self, event):
    newStdVols = [float(vol) for vol in re.findall(r"(?<!\d)\d+\.?\d*(?!\d)", event.widget.get("1.0", "end-1c"))]
    self.stdVols = newStdVols
    self.dataObject.updateStandards(self.volMixVar.get(), self.volTotalVar.get(), newStdVols)
    print(f"The volumeStandards have been updated to {newStdVols}")

  def __updateTracer(self, newTracer):
    self.dataObject.updateTracer(newTracer)

  def __updateTracerPurity(self, event):
    newPurity = [float(pur) for pur in re.findall(r"(?<!\d)\d+\.?\d*(?!\d)", event.widget.get("1.0", "end-1c"))]
    self.tracerPurity = newPurity
    self.dataObject.updateTracerPurity(newPurity)
    print(f"The tracer purity vector has been updated to {newPurity}")

  def __updateNACorrectionMethod(self, newMethod):
    self.dataObject.updateNACMethod(newMethod)

  def __updateVolumesForNormalization(self, newVolumeOfDilution, newVolumeOfSampleUsed, useValueDilution, useValueSample):
    self.dataObject.updateVolumesOfSampleDilution(newVolumeOfDilution, newVolumeOfSampleUsed, useValueDilution, useValueSample)

  def computeResults(self):
    self.dataObject.saveStandardCurvesAndResults()
    self.popupMsg("The results and plots have been saved.\nCheck out the standard plots.\nDo you want to modify the standards?")

  # --------------- Standard plots -------------------
  def inspectStandardPlots(self, popup):
    popup.destroy()
        
    selectFrame = tk.Tk()
    selectFrame.wm_title("FAMES standard to modify")
    label = ttk.Label(selectFrame, text="Select the LIPID(s) for which you want\n to correct the standard curve", font=("Verdana", 14))
    label.grid(row=1, column=1, columnspan=2, padx=10, pady=10)

    FAMESlistbox = tk.Listbox(selectFrame, height=10, selectmode='multiple')
    for item in self.dataObject._maskFAMES.keys():
      FAMESlistbox.insert(tk.END, item)
    FAMESlistbox.grid(row=2, column=1, columnspan=2, pady=10)

    FAMESbutton = ttk.Button(selectFrame, text="Select", command = lambda: self.modifySelection(selectFrame, FAMESlistbox))
    FAMESbutton.grid(row=3, column=1, columnspan=2, pady=10)

  def modifySelection(self, frameToKill, selection):
    FAMESselected = [selection.get(i) for i in selection.curselection()]

    # will go over all the selectedFAMES
    currentFAMESidx = 0

    fig,ax = plt.subplots(figsize=(4,3), constrained_layout=True)

    plotFrame = tk.Tk()
    plotFrame.wm_title("Standard curve inspector")

    def updateSelection(event):
      self.plotIsolatedFAMES(FAMESselected[currentFAMESidx], ax, canvas, pointsListbox, 0)

    pointsListbox = tk.Listbox(plotFrame, height=8, selectmode='multiple')
    pointsListbox.grid(row=1, column=3, columnspan=2, pady=10, padx=5)
    pointsListbox.bind("<<ListboxSelect>>", updateSelection)

    canvas = FigureCanvasTkAgg(fig, plotFrame)

    self.plotIsolatedFAMES(FAMESselected[currentFAMESidx], ax, canvas, pointsListbox, 1)

    figFrame = canvas.get_tk_widget()#.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)
    figFrame.grid(row=1, column=1, columnspan=2, rowspan=3, pady=10, padx=10)

    def goToNextPlot(direction):
      nonlocal currentFAMESidx
      if (currentFAMESidx+direction>=0) & (currentFAMESidx+direction<len(FAMESselected)):
        currentFAMESidx = currentFAMESidx+direction
        self.plotIsolatedFAMES(FAMESselected[currentFAMESidx], ax, canvas, pointsListbox, 1)
        nextButton["text"]="Next" # in case we come from last plot
        if currentFAMESidx == len(FAMESselected)-1:
          # last plot
          nextButton["text"]="Finish"
      elif (currentFAMESidx+direction<0):
        currentCorrectionIdx = 0
        print("You are already looking at the first standard plot!")
      else:
        quitCurrent()
        self.dataObject.saveStandardCurvesAndResults(useMask=True)
        self.popupMsg("The results and plots have been saved.\nCheck out the standard plots.\nDo you want to modify the standards?")

    if currentFAMESidx == len(FAMESselected)-1:
      textButton = "Send"
    else:
      textButton = "Next"
    nextButton = ttk.Button(plotFrame, text=textButton, command = lambda: goToNextPlot(1))
    nextButton.grid(row=2, column=4, pady=5)
    previousButton = ttk.Button(plotFrame, text="Previous", command = lambda: goToNextPlot(-1))
    previousButton.grid(row=2, column=3, pady=5)

    def quitCurrent():
      plt.close('all')
      plotFrame.destroy()

    plotButton3 = ttk.Button(plotFrame, text="Quit Inspector", command = quitCurrent)
    plotButton3.grid(row=3, column=3, columnspan=2, pady=5)

    frameToKill.destroy()


  def plotIsolatedFAMES(self, famesName, ax, canvas, pointsListbox, direction=1):
    ax.clear()

    try:
      xvals = self.dataObject.standardDf_nMoles[famesName].values
    except:
      # it means that nMoles from parental ion were used
      parentalIon = self.dataObject._checkIfParentalIonDataExistsFor(famesName)[1]
      xvals = self.dataObject.standardDf_nMoles[parentalIon].values
    yvals = self.dataObject.getStandardAbsorbance()[famesName].values

    if direction == 1:
      # if coming from another plot, start with a fresh ListBox
      pointsListbox.delete(0, tk.END)
      for i,(x,y) in enumerate(zip(xvals, yvals)):
        if y==0:
          # nan are converted to zero when initial cleaned dataDf is created, so just show
          # that those were in fact nans
          y = 'NAN'
        else:
          y = f"{y:.3f}"
        pointsListbox.insert(tk.END, f" point{i}: ({x:.3f}, {y})")

    maskSelected = [not (i in pointsListbox.curselection()) for i in range(len(xvals))]
    newMask = [(m1 & m2) for m1,m2 in zip(self.dataObject._maskFAMES[famesName]["originalMask"], maskSelected)]
    self.dataObject._maskFAMES[famesName]["newMask"] = newMask

    # select points that were invalid in original mask
    alreadyMaskedIndices = [i for i,boolean in enumerate(self.dataObject._maskFAMES[famesName]["originalMask"]) if boolean==False]
    for idx in alreadyMaskedIndices:
      pointsListbox.select_set(idx)

    if direction==0:
      # if we are still on the same FAMES, remember previous selection too
      alreadyMaskedIndices = [i for i,boolean in enumerate(newMask) if boolean==False]
      for idx in alreadyMaskedIndices:
        pointsListbox.select_set(idx)

    ax.plot(xvals[newMask], yvals[newMask], "o", color="#00BFFF")
    ax.plot(xvals[[not i for i in newMask]], yvals[[not i for i in newMask]], "o", color="#fb4c52")
    # slope,intercept = np.polyfit(np.array(xvals[newMask], dtype=float), np.array(yvals[newMask], dtype=float), 1)
    slope,intercept,rvalue,pvalue,stderr = stats.linregress(np.array(xvals[newMask], dtype=float), np.array(yvals[newMask], dtype=float))
    xfit = [np.min(xvals), np.max(xvals)]
    yfit = np.polyval([slope, intercept], xfit)
    # plot of data
    ax.plot(xfit, yfit, "-", color="#B4B4B4")
    ax.set_title(famesName)
    ax.set_xlabel("Quantity (nMoles)")
    ax.set_ylabel("Absorbance")

    canvas.draw()

  # --------------- Correction plots -------------------
  def inspectCorrectionPlots(self):

    def quitCurrent():
      plt.close('all')
      plotFrame.destroy()

    def goToNextPlot(direction):
      nonlocal currentCorrectionIdx
      if (currentCorrectionIdx+direction>=0) & (currentCorrectionIdx+direction<len(self.FANames)):
        currentCorrectionIdx = currentCorrectionIdx+direction
        self.plotIsolatedCorrection(self.FANames[currentCorrectionIdx], SampleList.get(), ax, canvas, correctionTreeView)
        nextButton["text"]="Next" # in case we come from last plot
        if currentCorrectionIdx == len(self.FANames)-1:
          # last plot
          nextButton["text"]="Finish"
      elif (currentCorrectionIdx+direction<0):
        currentCorrectionIdx = 0
        print("You are already looking at the first correction plot!")
      else:
        quitCurrent()

    def showNewSampleSelection(event):
      self.plotIsolatedCorrection(self.FANames[currentCorrectionIdx], SampleList.get(), ax, canvas, correctionTreeView)
    
    plotFrame = tk.Tk()
    plotFrame.wm_title("Natural Abundance Correction inspector")

    currentCorrectionIdx = 0 # will be used to go from an FAME to another

    # sample chooser
    SampleList = ttk.Combobox(plotFrame, height=6, state="readonly")
    SampleList.grid(row=1, column=1, columnspan=2)
    SampleList['values'] = [f"{name} - {sample}" for name,sample in self.dataObject.dataDf[["Name", "SampleName"]].values]
    SampleList.current(0)
    # somehow for this one I couldn't just link to a tk.variable and trace to get update working ...
    # so I directly bind to a function on change
    SampleList.bind("<<ComboboxSelected>>", showNewSampleSelection)

    # table display
    correctionTreeView = ttk.Treeview(plotFrame, columns=("original", "corrected"), height=15)
    correctionTreeView.heading('#0', text='')
    correctionTreeView.heading('#1', text='Original')
    correctionTreeView.heading('#2', text='Corrected')
    correctionTreeView.column("#0", width=50)
    correctionTreeView.column("#1", width=100, anchor=tk.E)
    correctionTreeView.column("#2", width=100, anchor=tk.E)
    correctionTreeView.grid(row=2, column=8, columnspan=3, pady=2, padx=10)

    # Main fig
    fig,ax = plt.subplots(figsize=(6,3), constrained_layout=True)
    canvas = FigureCanvasTkAgg(fig, plotFrame)
    self.plotIsolatedCorrection(self.FANames[currentCorrectionIdx], SampleList.get(), ax, canvas, correctionTreeView)
    figFrame = canvas.get_tk_widget()#.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)
    figFrame.grid(row=2, column=1, columnspan=7, rowspan=3, pady=10, padx=10)

    # buttons and others
    quitButton = ttk.Button(plotFrame, text="Quit", command = lambda: quitCurrent())
    quitButton.grid(row=5, column=1, pady=5)
    
    previousButton = ttk.Button(plotFrame, text="Previous", command = lambda: goToNextPlot(-1))
    previousButton.grid(row=5, column=6, pady=5)
    nextButton = ttk.Button(plotFrame, text="Next", command = lambda: goToNextPlot(1))
    nextButton.grid(row=5, column=7, pady=5)

    saveButton = ttk.Button(plotFrame, text="Save all plots", command = lambda: self.saveAllCorrectionPlots())
    saveButton.grid(row=5, column=10, columnspan=2, pady=5)

  def plotIsolatedCorrection(self, famesName, sampleName, ax, canvas, treeView):
    ax.clear()

    # get row associated with sampleName provided
    row = np.where(self.dataObject.dataDf["Name"] == sampleName.split(" ")[0])[0][0]

    originalData = self.dataObject.dataDf.filter(like=famesName).iloc[row]
    correctedData = self.dataObject.dataDf_corrected.filter(like=famesName).iloc[row]

    # make x label
    xLabels = [f"M.{i}" for i in range(len(originalData))]
    xrange = np.arange(len(originalData))
    barWidth = 0.4

    ax.bar(xrange-barWidth/2, originalData, barWidth, color="#B4B4B4", label="Original")
    ax.bar(xrange+barWidth/2, correctedData, barWidth, color="#00BFFF", label="Corrected")
    ax.set_xticks(xrange)
    ax.set_xticklabels(xLabels)
    ax.legend()

    ax.set_ylabel("Absorbance")
    ax.set_title(famesName)

    canvas.draw()

    # clear and update table
    treeView.delete(*treeView.get_children())
    for i,(x,y) in enumerate(zip(originalData, correctedData)):
      treeView.insert("" , i, text=f"M.{i}", values=(f"{x:.0f}", f"{y:.1f}"))

  def saveAllCorrectionPlots(self):
    # create folder if it doesn't exist
    directory = f"{self.dataObject.pathDirName}/correctionPlots"
    if not os.path.exists(directory):
      os.mkdir(directory)

    for name in self.FANames:

      originalData = self.dataObject.dataDf.filter(like=name)
      correctedData = self.dataObject.dataDf_corrected.filter(like=name)
      
      # if only one column, it means it was not a Fames with non parental ions
      if len(originalData.columns)==1:
        continue

      # create folder if doesn't exist
      directory = f"{self.dataObject.pathDirName}/correctionPlots/{'-'.join(name.split(':'))}"
      if not os.path.exists(directory):
        os.mkdir(directory)

      fig,ax = plt.subplots(figsize=(6,3), constrained_layout=True)

      for i in range(len(originalData)):
        orData = originalData.iloc[i]
        corData = correctedData.iloc[i]

        # make x labels
        xLabels = [f"M.{i}" for i in range(len(orData))]
        xrange = np.arange(len(orData))
        barWidth = 0.4

        # clear axe each time
        ax.clear()
        ax.bar(xrange-barWidth/2, orData, barWidth, color="#B4B4B4", label="Original")
        ax.bar(xrange+barWidth/2, corData, barWidth, color="#00BFFF", label="Corrected")
        ax.set_xticks(xrange)
        ax.set_xticklabels(xLabels)
        ax.legend()

        ax.set_title(f"{name} - {self.dataObject.dataDf.iloc[i, 2]} {self.dataObject.dataDf.iloc[i, 3]}")
        ax.set_ylabel("Absorbance")

        fig.savefig(f"{directory}/{self.dataObject.dataDf.iloc[i, 2]}")
      plt.close("all")


############################
## MAIN
############################

if __name__ == '__main__':

  ######################
  ## Please ignore, this is just a facility hack to call the dvt mode and testing via ipython without the GUI 
  if len(sys.argv) == 3: #dvt mode
    if sys.argv[2] != "Labeled":
      print("Dvt: Not Labeled expt")
      # not labeled ex
      # filenames = ["data/ex-data-not-labeled.xlsx", "data/template_not_labeled.xlsx"]
      # filenames = ["data2/171125DHAmilk2.xlsx", "data2/template.xlsx"]
      # filenames = ["data/example-unlabeled-expt/expt-not-labeled.xlsx", "data/example-unlabeled-expt/template_not_labeled.xlsx"]
      filenames = ["Dylan/_chol-2/template-Lung Pilot.xlsx", "Dylan/_chol-2/LungPilot-CHOL-Parental ion only.xlsx"]

      appData = MSDataContainer(filenames)
      try:
        newInternalRef = [name for name in appData.internalRefList if appData.internalRef in name][0]
      except:
        newInternalRef = appData.internalRefList[-1]
      appData.updateInternalRef(newInternalRef)
      # appData.updateStandards(244, 250, [1, 5, 10, 20, 40, 80])
      appData.computeStandardFits()
    else:
      print("Dvt: Labeled expt")
      # labeled
      # filenames = ["data/ex-data-labeled.xlsx", "data/template_labeled.xlsx"]
      # appData = MSDataContainer(filenames)
      # appData.updateStandards(40, 500, [1, 5, 10, 20, 40, 80])
      # appData.computeNACorrectionDf()
      # appData.dataDf_norm = appData.computeNormalizedData()
      # appData.computeStandardFits()
      # filenames = ["data/example-cholesterol/expt-chol.xlsx", "data/example-cholesterol/template.xlsx"]
      filenames = ["data/example-labeled-nonparental/180808ETV39_LungFAMES.xlsx", "data/example-labeled-nonparental/template-ETV39_LungFAMES.xlsx"]

      appData = MSDataContainer(filenames)
      try:
        newInternalRef = [name for name in appData.internalRefList if appData.internalRef in name][0]
      except:
        newInternalRef = appData.internalRefList[-1]
      appData.updateInternalRef(newInternalRef)
      appData.updateStandards(244, 250, [1, 5, 10, 20])
      appData.updateTracer("H")

  ##########################################
  ## This is what __main__ should look like
  else:
    # Is the directory to look in for data files defined?
    if len(sys.argv) == 1: # no arguments given to the function
      initialDirectory = False
    else:
      initialDirectory = sys.argv[1]

    # Choose data and template files
    fileNames = initialFileChoser(initialDirectory)


    # The container that will hold all the data
    appData = MSDataContainer(fileNames)

    print(f"""Two files have been loaded:
      \tData file: {appData.dataFileName}
      \tTemplate file: {appData.templateFileName}""")
    print(f"The experiment type detected is '{appData.experimentType}'")
    if appData._cholesterol:
      print("The files loaded have CHOLESTEROL data")


    # Create the entire GUI program and pass in colNames for popup menu
    app = MSAnalyzer(appData)

    # Start the GUI event loop
    app.window.mainloop()


